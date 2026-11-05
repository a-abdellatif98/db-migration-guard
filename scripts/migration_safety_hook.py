#!/usr/bin/env python3
"""PreToolUse hook: blocks DANGEROUS migration changes, surfaces WARNING ones without blocking."""

import glob
import json
import os
import re
import sys
import tempfile
import time

STATE_MAX_AGE_SECONDS = 7 * 86400

DANGEROUS = "DANGEROUS"
WARNING = "WARNING"

MESSAGES = {
    "drop_column": (
        DANGEROUS,
        "Dropping a column causes irreversible data loss.\n\n"
        "Safe pattern:\n"
        "1. Stop writing to the column in app code\n"
        "2. Deploy code that no longer reads from it\n"
        "3. Remove the column in a separate migration\n"
        "4. Keep a backup migration to restore if needed",
    ),
    "drop_table": (
        DANGEROUS,
        "Dropping a table deletes all data permanently.\n\n"
        "Before dropping:\n"
        "- Confirm no code references this table\n"
        "- Back up the data if needed\n"
        "- Consider renaming instead of dropping as a first step",
    ),
    "rename_column": (
        DANGEROUS,
        "Renaming causes downtime during rolling deployments, "
        "old code still references the old name.\n\n"
        "Safe pattern:\n"
        "1. Add the new column\n"
        "2. Backfill data from old to new\n"
        "3. Update app code to use new column\n"
        "4. Drop the old column in a later migration",
    ),
    "change_type": (
        DANGEROUS,
        "Changing a column type may lock the table and fail "
        "if existing data is incompatible.\n\n"
        "- Test with production-like data first\n"
        "- For large tables, use add/backfill/swap/drop instead\n"
        "- Check data compatibility before applying",
    ),
    "remove_index": (
        DANGEROUS,
        "Removing an index may cause severe query performance degradation.\n\n"
        "Before removing:\n"
        "- Verify no queries depend on this index\n"
        "- Check slow query logs or run EXPLAIN\n"
        "- Consider the index's role in foreign key lookups",
    ),
    "not_null_no_default": (
        WARNING,
        "Adding a NOT NULL column without a default fails on tables with existing rows.\n\n"
        "Safe pattern:\n"
        "1. Add the column as nullable\n"
        "2. Backfill existing rows\n"
        "3. Add the NOT NULL constraint in a separate migration",
    ),
    "non_concurrent_index": (
        WARNING,
        "Adding an index without CONCURRENTLY locks the table for writes.\n\n"
        "Use concurrent index creation:\n"
        "- Rails: add_index :table, :col, algorithm: :concurrently "
        "(with disable_ddl_transaction!)\n"
        "- Django: AddIndexConcurrently\n"
        "- SQL: CREATE INDEX CONCURRENTLY",
    ),
    "unique_constraint": (
        WARNING,
        "A unique constraint fails if duplicate values already exist.\n\n"
        "- Query for duplicates first and resolve them\n"
        "- Add the constraint in a separate migration after cleanup",
    ),
    "data_in_schema_migration": (
        WARNING,
        "Mixing data changes and schema changes in one migration can leave the "
        "database inconsistent if it fails partway.\n\n"
        "- Split the backfill into its own migration\n"
        "- Make data migrations idempotent and reversible",
    ),
    "fk_validation": (
        WARNING,
        "Adding a foreign key to an existing table checks every row and blocks writes while it does.\n\n"
        "- PostgreSQL: add it NOT VALID, then VALIDATE CONSTRAINT in a separate migration "
        "(Rails: validate: false, then validate_foreign_key)\n"
        "- MySQL: check for orphaned rows first; for large tables use pt-online-schema-change",
    ),
    "check_validation": (
        WARNING,
        "Adding a CHECK constraint scans every row under a write-blocking lock.\n\n"
        "- PostgreSQL: add it NOT VALID, then VALIDATE CONSTRAINT in a separate migration\n"
        "- MySQL: fix violating rows first; for large tables use gh-ost or pt-online-schema-change",
    ),
    "set_not_null": (
        WARNING,
        "Making an existing column NOT NULL scans the table under a write-blocking lock.\n\n"
        "- PostgreSQL 12+: add CHECK (col IS NOT NULL) NOT VALID, validate it, then SET NOT NULL\n"
        "- MySQL: backfill NULLs first; use an online schema change tool for large tables",
    ),
    "concurrently_on_mysql": (
        WARNING,
        "CONCURRENTLY is PostgreSQL-only and fails on MySQL.\n\n"
        "InnoDB already builds secondary indexes online. Use ALGORITHM=INPLACE, LOCK=NONE "
        "(Rails: algorithm: :inplace, Django: plain AddIndex).",
    ),
    "mysql_blocking_algorithm": (
        WARNING,
        "ALGORITHM=COPY or LOCK=SHARED/EXCLUSIVE blocks writes for the whole operation.\n\n"
        "- Prefer ALGORITHM=INSTANT or INPLACE with LOCK=NONE where MySQL supports it\n"
        "- Otherwise use gh-ost or pt-online-schema-change",
    ),
    "unique_without_tenant": (
        WARNING,
        "This project scopes rows by tenant, but this unique index does not include the tenant "
        "column, so two tenants cannot store the same value.\n\n"
        "- Put the tenant column first, e.g. add_index :users, [:account_id, :email], unique: true",
    ),
}

ENGINE_MESSAGES = {
    "postgresql": {
        "non_concurrent_index": (
            "Adding an index without CONCURRENTLY blocks writes to the table until the build finishes.\n\n"
            "- Rails: add_index :table, :col, algorithm: :concurrently (with disable_ddl_transaction!)\n"
            "- Django: AddIndexConcurrently (in a migration with atomic = False)\n"
            "- SQL: CREATE INDEX CONCURRENTLY (outside a transaction)"
        ),
        "change_type": (
            "Most column type changes rewrite the whole table under an ACCESS EXCLUSIVE lock "
            "(widening a varchar or varchar to text are exceptions).\n\n"
            "- Test with production-like data first\n"
            "- For large tables, use add/backfill/swap/drop instead"
        ),
        "fk_validation": (
            "Adding a foreign key takes a SHARE ROW EXCLUSIVE lock on both tables and checks every "
            "existing row, blocking writes until it finishes.\n\n"
            "Safe pattern:\n"
            "1. Add it without validation: SQL NOT VALID, Rails add_foreign_key ..., validate: false\n"
            "2. Validate in a separate migration: VALIDATE CONSTRAINT, Rails validate_foreign_key"
        ),
        "check_validation": (
            "Adding a CHECK constraint holds an ACCESS EXCLUSIVE lock while it scans every row.\n\n"
            "Safe pattern:\n"
            "1. Add it NOT VALID (Rails: add_check_constraint ..., validate: false)\n"
            "2. VALIDATE CONSTRAINT in a separate migration (Rails: validate_check_constraint)"
        ),
        "set_not_null": (
            "SET NOT NULL scans the whole table under an ACCESS EXCLUSIVE lock.\n\n"
            "Safe pattern (PostgreSQL 12+):\n"
            "1. ADD CONSTRAINT c CHECK (col IS NOT NULL) NOT VALID\n"
            "2. VALIDATE CONSTRAINT c in a separate migration\n"
            "3. SET NOT NULL, which now skips the scan\n"
            "4. DROP CONSTRAINT c"
        ),
    },
    "mysql": {
        "non_concurrent_index": (
            "InnoDB builds secondary indexes online, but on large tables the build can lag replicas "
            "and has to wait for a metadata lock.\n\n"
            "- State the algorithm so MySQL fails instead of locking: ALGORITHM=INPLACE, LOCK=NONE "
            "(Rails: algorithm: :inplace)\n"
            "- For very large tables use gh-ost or pt-online-schema-change"
        ),
        "change_type": (
            "Changing a column type usually copies the table (ALGORITHM=COPY) and blocks writes "
            "for the whole copy.\n\n"
            "- Check existing data fits the new type\n"
            "- For large tables use gh-ost or pt-online-schema-change"
        ),
        "not_null_no_default": (
            "Adding a NOT NULL column without a default does not fail on MySQL: existing rows "
            "silently get the type's implicit default ('' for strings, 0 for numbers).\n\n"
            "- Give it an explicit DEFAULT, or\n"
            "- Add it nullable, backfill, then make it NOT NULL"
        ),
        "fk_validation": (
            "Adding a foreign key copies the whole table and blocks writes, unless "
            "foreign_key_checks is off (then InnoDB adds it in place without checking existing rows).\n\n"
            "- Check for orphaned rows first\n"
            "- For large tables use pt-online-schema-change (gh-ost does not support foreign keys)"
        ),
        "check_validation": (
            "MySQL has no NOT VALID, so adding a CHECK constraint checks every existing row and may "
            "need a table copy that blocks writes.\n\n"
            "- Fix violating rows first\n"
            "- For large tables use gh-ost or pt-online-schema-change"
        ),
        "set_not_null": (
            "Changing a column to NOT NULL rebuilds the table (in place, writes still allowed) and fails "
            "if any NULLs remain. On large tables the rebuild is slow and lags replicas.\n\n"
            "- Backfill NULLs first, in batches\n"
            "- Use ALGORITHM=INPLACE, LOCK=NONE, or gh-ost / pt-online-schema-change for large tables"
        ),
    },
}

ENGINE_LABEL = {"postgresql": "PostgreSQL", "mysql": "MySQL"}

RULE_ORDER = list(MESSAGES)

SQL = {
    "drop_column": r"(?i:\bDROP\s+COLUMN\b)",
    "drop_table": r"(?i:\bDROP\s+TABLE\b)",
    "rename_column": r"(?i:\bRENAME\s+(?:COLUMN|TO|TABLE)\b)",
    "change_type": r"(?i:\bALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b)",
    "remove_index": r"(?i:\bDROP\s+INDEX\b)",
    "non_concurrent_index": r"(?i:\bCREATE\s+(?:UNIQUE\s+)?INDEX\b(?!\s+CONCURRENTLY\b)"
                            r"|\bADD\s+(?:UNIQUE\s+)?(?:INDEX|KEY)\b)",
    "unique_constraint": r"(?i:\bADD\s+(?:CONSTRAINT\s+\S+\s+)?UNIQUE\b|\bCREATE\s+UNIQUE\s+INDEX\b)",
    "set_not_null": r"(?i:\bALTER\s+COLUMN\s+\S+\s+SET\s+NOT\s+NULL\b)",
    "mysql_blocking_algorithm": r"(?i:\bALGORITHM\s*=\s*COPY\b|\bLOCK\s*=\s*(?:SHARED|EXCLUSIVE)\b)"
                                r"|\balgorithm:\s*:copy\b",
}

TENANT_COLUMN = re.compile(r"\b(?:tenant|account|organi[sz]ation|org|company|workspace|team)_id\b")
NOT_VALID = re.compile(r"(?i)\bNOT\s+VALID\b")
MYSQL_ONLINE = re.compile(r"(?i)\bALGORITHM\s*=\s*(?:INPLACE|INSTANT)\b|\balgorithm:\s*:(?:inplace|instant)\b")
CONCURRENTLY = re.compile(r"(?i)\bCONCURRENTLY\b|\balgorithm:\s*:concurrently\b|\bAddIndexConcurrently\b")

SYNTAX = {
    "rails": {
        "drop_column": r"\b(?:remove_columns?|drop_column)\b",
        "drop_table": r"\bdrop_table\b",
        "rename_column": r"\b(?:rename_column|rename_table)\b",
        "change_type": r"\bchange_column\b",
        "remove_index": r"\bremove_index\b",
    },
    "django": {
        "drop_column": r"\bRemoveField\b",
        "drop_table": r"\bDeleteModel\b",
        "rename_column": r"\b(?:RenameField|RenameModel)\b",
        "change_type": r"\bAlterField\b",
        "remove_index": r"\bRemoveIndex(?:Concurrently)?\b",
        "non_concurrent_index": r"\bAddIndex\b(?=\s*\()",
    },
    "laravel": {
        "drop_column": r"\bdropColumns?\b",
        "drop_table": r"\bSchema::(?:drop|dropIfExists)\s*\(|\bdropTable\b",
        "rename_column": r"\b(?:renameColumn|Schema::rename)\b",
        "change_type": r"->\s*change\s*\(\s*\)",
        "remove_index": r"\b(?:dropIndex|dropUnique)\b",
    },
    "knex": {
        "drop_column": r"\bdropColumns?\b",
        "drop_table": r"\b(?:dropTable|dropTableIfExists)\b",
        "rename_column": r"\b(?:renameColumn|renameTable)\b",
        "change_type": r"\.alter\s*\(\s*\)",
        "remove_index": r"\b(?:dropIndex|dropUnique)\b",
    },
    "prisma": {},
}

COMMENT_PREFIX = {"rails": "#", "django": "#", "laravel": "//", "knex": "//", "prisma": "--"}
SQL_ADD_CLAUSE = re.compile(
    r"(?i)\bADD\s+(?:COLUMN\s+)?(?!CONSTRAINT\b|PRIMARY\b|FOREIGN\b|UNIQUE\b|CHECK\b|INDEX\b|KEY\b)"
)
SQL_IMPLICIT_DEFAULT = re.compile(r"(?i)\b(?:DEFAULT|SERIAL|BIGSERIAL|GENERATED|AUTO_INCREMENT|IDENTITY)\b")

DATA_OPS = {
    "rails": r"\.(?:update_all|update_columns?|find_each|find_in_batches|delete_all|destroy_all)\b"
             r"|\bexecute\b[^\n]*(?i:\b(?:UPDATE\s+\S+\s+SET|INSERT\s+INTO|DELETE\s+FROM)\b)",
    "django": r"\bRunPython\b",
    "laravel": r"\bDB::(?:update|insert|delete)\s*\(|\bDB::table\([^;]*?->(?:update|insert|delete)\s*\(",
}
SCHEMA_OPS = {
    "rails": r"\b(?:add_column|create_table|change_table|add_index|add_reference|remove_columns?|"
             r"change_column|rename_column|drop_table)\b|(?i:\bALTER\s+TABLE\b)",
    "django": r"\b(?:AddField|CreateModel|AlterField|RemoveField|RenameField|AddIndex|DeleteModel)\b",
    "laravel": r"\bSchema::(?:create|table)\b",
}


def framework_for(path):
    p = path.replace("\\", "/")
    if "db/migrate/" in p and p.endswith(".rb"):
        return "rails"
    if "database/migrations/" in p and p.endswith(".php"):
        return "laravel"
    if "prisma/migrations/" in p and p.endswith(".sql"):
        return "prisma"
    if "migrations/" in p and p.endswith(".py") and not p.endswith("__init__.py"):
        return "django"
    if "migrations/" in p and p.endswith((".js", ".ts")):
        return "knex"
    return None


PROJECT_FILES = [
    # lock and dependency files only: database config files can hold passwords
    "Gemfile.lock", "composer.json",
    "package.json", "requirements.txt", "requirements/base.txt", "requirements/production.txt",
    "pyproject.toml", "Pipfile", "poetry.lock", "migration_lock.toml",
]
PYTHON_DEP_FILES = ["requirements.txt", "requirements/base.txt", "requirements/production.txt",
                    "pyproject.toml", "Pipfile", "poetry.lock"]
MAX_LEVELS = 10
MAX_READ = 1 << 20

ADAPTERS = {
    "postgresql": "postgresql", "postgis": "postgresql", "postgres": "postgresql", "pgsql": "postgresql",
    "mysql": "mysql", "mysql2": "mysql", "trilogy": "mysql", "mariadb": "mysql",
}


def read_text(path):
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read(MAX_READ)
    except OSError:
        return None


def project_files(file_path, cwd):
    """Nearest copy of each known project file, walking up from the migration to the git root or cwd."""
    p = file_path.replace("\\", "/")
    if not os.path.isabs(p) and cwd:
        p = os.path.join(cwd, p)
    d = os.path.dirname(os.path.abspath(p))
    stop = os.path.abspath(cwd) if cwd else None
    found = {}
    for _ in range(MAX_LEVELS):
        for rel in PROJECT_FILES:
            if rel not in found:
                text = read_text(os.path.join(d, rel))
                if text is not None:
                    found[rel] = text
        parent = os.path.dirname(d)
        if os.path.isdir(os.path.join(d, ".git")) or d == stop or parent == d:
            break
        d = parent
    return found


def json_deps(text, *keys):
    try:
        data = json.loads(text or "")
    except ValueError:
        return set()
    if not isinstance(data, dict):
        return set()
    deps = set()
    for key in keys:
        section = data.get(key)
        if isinstance(section, dict):
            deps.update(section)
    return deps


def only_engine(candidates):
    candidates.discard(None)
    return candidates.pop() if len(candidates) == 1 else None


def detect_engine(files):
    prisma = re.search(r'provider\s*=\s*"(\w+)"', files.get("migration_lock.toml", ""))
    if prisma and ADAPTERS.get(prisma.group(1)):
        return ADAPTERS[prisma.group(1)]

    deps = set()
    for m in re.finditer(r"(?m)^ {4}(pg|mysql2|trilogy) \(", files.get("Gemfile.lock", "")):
        deps.add("postgresql" if m.group(1) == "pg" else "mysql")
    python = "\n".join(files.get(f, "") for f in PYTHON_DEP_FILES)
    for m in re.finditer(r"(?im)^\s*[\"']?(psycopg2?(?:-binary)?|mysqlclient|pymysql|mysql-connector-python)\b",
                         python):
        deps.add("postgresql" if m.group(1).lower().startswith("psycopg") else "mysql")
    for name in json_deps(files.get("package.json"), "dependencies", "devDependencies"):
        if name == "pg":
            deps.add("postgresql")
        elif name in ("mysql", "mysql2"):
            deps.add("mysql")
    return only_engine(deps)


TENANCY_LIBS = [
    ("gem", "ros-apartment", "schema", "bundle exec rake apartment:migrate"),
    ("gem", "apartment", "schema", "bundle exec rake apartment:migrate"),
    ("python", "django-tenants", "schema", "python manage.py migrate_schemas"),
    ("python", "django-tenant-schemas", "schema", "python manage.py migrate_schemas"),
    ("composer", "stancl/tenancy", "schema", "php artisan tenants:migrate"),
    ("composer", "spatie/laravel-multitenancy", "schema", None),
    ("gem", "acts_as_tenant", "row", None),
]


def detect_tenancy(files, file_path):
    python = "\n".join(files.get(f, "") for f in PYTHON_DEP_FILES)
    composer = json_deps(files.get("composer.json"), "require", "require-dev")
    for source, lib, kind, command in TENANCY_LIBS:
        if source == "gem":
            hit = re.search(rf"(?m)^ {{4}}{re.escape(lib)} \(", files.get("Gemfile.lock", ""))
        elif source == "python":
            hit = re.search(rf"(?im)^\s*[\"']?{re.escape(lib)}\b", python)
        else:
            hit = lib in composer
        if hit:
            return {"kind": kind, "lib": lib, "command": command}
    if "/migrations/tenant/" in file_path.replace("\\", "/"):
        return {"kind": "schema", "lib": "tenant migrations folder", "command": None}
    return None


REHEARSE_COMMANDS = {
    "rails": "RAILS_ENV=sentinel bin/rails db:migrate  (with a sentinel entry in database.yml)",
    "django": "python manage.py migrate --database=sentinel  (with a sentinel alias in DATABASES)",
    "laravel": "php artisan migrate --database=sentinel  (with a sentinel connection in config/database.php)",
    "prisma": "npx prisma migrate deploy  (from a shell configured for the sentinel copy)",
    "knex": "npx knex migrate:latest --env sentinel  (with a sentinel environment in the knexfile)",
}


def rehearsal_note(fw):
    return ("Rehearse first on a sentinel database: a restored copy of production, never production "
            "itself, set up as its own named connection. Then run and time:\n"
            f"  {REHEARSE_COMMANDS[fw]}")


def context_notes(names, fw, engine, tenancy, content):
    notes = []
    if any(MESSAGES[n][0] == DANGEROUS for n in names):
        notes.append(rehearsal_note(fw))
    if engine == "postgresql" and not re.search(r"(?i)lock_timeout", content):
        notes.append("PostgreSQL: set a lock_timeout (e.g. SET lock_timeout = '5s') so blocked DDL fails "
                     "fast instead of stalling every query queued behind it. Rails: strong_migrations "
                     "can set it for you.")
    if engine == "mysql":
        notes.append("MySQL: DDL is not transactional, so if a later statement fails the earlier ones stay "
                     "applied. Keep one DDL change per migration, and set lock_wait_timeout so a blocked "
                     "ALTER fails fast.")
    if tenancy and tenancy["kind"] == "schema":
        lines = [f"Multi-tenant ({tenancy['lib']}): this migration runs once per tenant schema or database. "
                 "Lock time and runtime multiply by the tenant count, and a failure partway leaves "
                 "tenants on different versions.",
                 "- Make it idempotent and safe to re-run",
                 "- Batch backfills per tenant",
                 "- Try it on one tenant first"]
        if tenancy["command"]:
            lines.append(f"- Run with: {tenancy['command']}")
        notes.append("\n".join(lines))
    return notes


def strip_comments(content, fw):
    prefix = COMMENT_PREFIX[fw]
    kept = []
    for line in content.splitlines():
        stripped = line.lstrip()
        if stripped.startswith(prefix):
            continue
        if fw == "prisma":
            line = line.split("--", 1)[0]
        kept.append(line)
    return "\n".join(kept)


def strip_ruby_down(content):
    lines = content.splitlines()
    out, i = [], 0
    while i < len(lines):
        m = re.match(r"(\s*)def\s+down\b", lines[i])
        if not m:
            out.append(lines[i])
            i += 1
            continue
        indent = len(m.group(1))
        i += 1
        while i < len(lines):
            cur = len(lines[i]) - len(lines[i].lstrip())
            if lines[i].strip() and cur <= indent:
                i += 1
                break
            i += 1
    return "\n".join(out)


def strip_brace_down(content, start_re):
    while True:
        m = re.search(start_re, content)
        if not m:
            return content
        brace = content.find("{", m.end())
        semi = content.find(";", m.end())
        if brace == -1 or (semi != -1 and semi < brace):
            end = len(content) if semi == -1 else semi + 1
        else:
            depth, end = 0, len(content)
            for idx in range(brace, len(content)):
                if content[idx] == "{":
                    depth += 1
                elif content[idx] == "}":
                    depth -= 1
                    if depth == 0:
                        end = idx + 1
                        break
        content = content[:m.start()] + content[end:]


def strip_down(content, fw):
    if fw == "rails":
        return strip_ruby_down(content)
    if fw == "laravel":
        return strip_brace_down(content, r"function\s+down\s*\(")
    if fw == "knex":
        return strip_brace_down(content, r"exports\.down\s*=|(?:async\s+)?function\s+down\b")
    return content


def ruby_statements(content):
    joined, buf = [], ""
    for line in content.splitlines():
        buf = f"{buf} {line.strip()}" if buf else line.strip()
        if not buf.endswith((",", "\\")):
            joined.append(buf)
            buf = ""
    if buf:
        joined.append(buf)
    return joined


def balanced_calls(content, name):
    calls = []
    for m in re.finditer(rf"\b{name}\s*\(", content):
        depth, i = 1, m.end()
        while i < len(content) and depth:
            depth += {"(": 1, ")": -1}.get(content[i], 0)
            i += 1
        calls.append(content[m.start():i])
    return calls


def sql_chunks(content, fw):
    pieces = re.split(r";", content) if fw == "prisma" else re.split(r"[;\n]", content)
    return [p for p in pieces if p.strip()]


def sql_adds_not_null_column(content, fw):
    for chunk in sql_chunks(content, fw):
        m = re.search(r"(?i)\bALTER\s+TABLE\b", chunk)
        if not m:
            continue
        for clause in re.split(r"(?i),\s*(?=ADD\b)", chunk[m.end():]):
            if (SQL_ADD_CLAUSE.search(clause) and re.search(r"(?i)\bNOT\s+NULL\b", clause)
                    and not SQL_IMPLICIT_DEFAULT.search(clause)):
                return True
    return False


def php_alter_statements(content):
    """Statements that sit inside a Schema::table closure (not Schema::create)."""
    contexts = [(m.start(), m.group(1)) for m in re.finditer(r"\bSchema::(create|table)\s*\(", content)]
    result, pos = [], 0
    for chunk in content.split(";"):
        start = pos
        pos += len(chunk) + 1
        before = [kind for at, kind in contexts if at <= start + len(chunk)]
        if before and before[-1] == "table":
            result.append(chunk)
    return result


def sql_statements(content):
    return [s for s in content.split(";") if s.strip()]


def detect_sql_constraints(content, found, unique_stmts, fw):
    # outside pure SQL files, only uppercase keywords count, so Ruby's t.references is ignored
    fk = re.compile(r"\bFOREIGN\s+KEY\b|\bREFERENCES\b", re.I if fw == "prisma" else 0)
    for stmt in sql_statements(content):
        if not re.search(r"(?i)\bALTER\s+TABLE\b", stmt):
            if re.search(r"(?i)\bCREATE\s+UNIQUE\s+INDEX\b", stmt):
                unique_stmts.append(stmt)
            continue
        if fk.search(stmt) and not NOT_VALID.search(stmt):
            found.add("fk_validation")
        if re.search(r"(?i)\bADD\s+(?:CONSTRAINT\s+\S+\s+)?CHECK\b", stmt) and not NOT_VALID.search(stmt):
            found.add("check_validation")
        if re.search(r"(?i)\bADD\s+(?:CONSTRAINT\s+\S+\s+)?UNIQUE\b", stmt):
            unique_stmts.append(stmt)


def rails_index_is_flagged(stmt, engine):
    if engine == "mysql":
        return not MYSQL_ONLINE.search(stmt) and not CONCURRENTLY.search(stmt)
    return not CONCURRENTLY.search(stmt)


def detect(fw, content, engine=None, tenancy=None):
    found = set()
    unique_stmts = []
    syntax = SYNTAX[fw]
    for name in RULE_ORDER:
        for rx in (syntax.get(name), SQL.get(name)):
            if rx and re.search(rx, content):
                found.add(name)
    detect_sql_constraints(content, found, unique_stmts, fw)

    if fw == "rails":
        for stmt in ruby_statements(content):
            not_validated = not re.search(r"\bvalidate:\s*false\b", stmt)
            if re.search(r"\badd_index\b", stmt):
                if rails_index_is_flagged(stmt, engine):
                    found.add("non_concurrent_index")
                if re.search(r"\bunique:\s*true\b", stmt):
                    found.add("unique_constraint")
                    unique_stmts.append(stmt)
            if (re.search(r"\badd_column\b", stmt) and re.search(r"\bnull:\s*false\b", stmt)
                    and not re.search(r"\bdefault:", stmt)):
                found.add("not_null_no_default")
            if re.search(r"\badd_foreign_key\b", stmt) and not_validated:
                found.add("fk_validation")
            if (re.search(r"\badd_(?:reference|belongs_to)\b", stmt)
                    and re.search(r"\bforeign_key:\s*(?:true|\{)", stmt) and not_validated):
                found.add("fk_validation")
            if re.search(r"\badd_check_constraint\b", stmt) and not_validated:
                found.add("check_validation")
            if re.search(r"\bchange_column_null\b[^\n]*,\s*false\b", stmt):
                found.add("set_not_null")
    elif fw == "django":
        for call in balanced_calls(content, "AddField"):
            if re.search(r"\bnull\s*=\s*False\b", call) and not re.search(r"\bdefault\s*=", call):
                found.add("not_null_no_default")
            if (re.search(r"\b(?:ForeignKey|OneToOneField)\s*\(", call)
                    and not re.search(r"\bdb_constraint\s*=\s*False\b", call)):
                found.add("fk_validation")
        for call in balanced_calls(content, "AddField") + balanced_calls(content, "AlterField"):
            if re.search(r"\bunique\s*=\s*True\b", call):
                found.add("unique_constraint")
                unique_stmts.append(call)
        for call in balanced_calls(content, "AddConstraint"):
            if re.search(r"\bCheckConstraint\b", call):
                found.add("check_validation")
            if re.search(r"\bUniqueConstraint\b", call):
                found.add("unique_constraint")
                unique_stmts.append(call)
    elif fw == "laravel":
        for stmt in php_alter_statements(content):
            if re.search(r"->\s*nullable\s*\(\s*false\s*\)", stmt) and not re.search(r"->\s*default\s*\(", stmt):
                found.add("not_null_no_default")
            if re.search(r"->\s*unique\s*\(", stmt):
                found.add("unique_constraint")
                unique_stmts.append(stmt)
            if re.search(r"->\s*(?:foreign|constrained)\s*\(", stmt):
                found.add("fk_validation")

    if sql_adds_not_null_column(content, fw):
        found.add("not_null_no_default")

    if fw in DATA_OPS and re.search(DATA_OPS[fw], content) and re.search(SCHEMA_OPS[fw], content):
        found.add("data_in_schema_migration")

    if engine == "mysql":
        if CONCURRENTLY.search(content):
            found.add("concurrently_on_mysql")
            if fw != "rails":
                found.discard("non_concurrent_index")
        elif fw != "rails" and MYSQL_ONLINE.search(content):
            found.discard("non_concurrent_index")
    elif engine == "postgresql":
        found.discard("mysql_blocking_algorithm")

    if tenancy and tenancy["kind"] == "row" and any(not TENANT_COLUMN.search(s) for s in unique_stmts):
        found.add("unique_without_tenant")

    return [name for name in RULE_ORDER if name in found]


def check_content(content, fw, engine=None, tenancy=None):
    return detect(fw, strip_down(strip_comments(content, fw), fw), engine, tenancy)


def state_dir():
    return os.path.join(tempfile.gettempdir(), "db-migration-guard")


def state_file(session_id):
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", str(session_id))
    return os.path.join(state_dir(), f"migration_guard_state_{safe}.json")


def prune_old_state():
    cutoff = time.time() - STATE_MAX_AGE_SECONDS
    for path in glob.glob(os.path.join(state_dir(), "migration_guard_state_*.json")):
        try:
            if os.path.getmtime(path) < cutoff:
                os.remove(path)
        except OSError:
            pass


def load_shown(session_id):
    try:
        with open(state_file(session_id)) as f:
            return set(json.load(f))
    except (OSError, ValueError, TypeError):
        return set()


def save_shown(session_id, shown):
    try:
        os.makedirs(state_dir(), exist_ok=True)
        with open(state_file(session_id), "w") as f:
            json.dump(sorted(shown), f)
        prune_old_state()
    except OSError:
        pass


def extract_content(tool_name, tool_input):
    if tool_name == "Write":
        return tool_input.get("content") or ""
    if tool_name == "Edit":
        return tool_input.get("new_string") or ""
    if tool_name == "MultiEdit":
        edits = tool_input.get("edits") or []
        return "\n".join((e.get("new_string") or "") for e in edits if isinstance(e, dict))
    return ""


def message_for(name, engine):
    level, generic = MESSAGES[name]
    return level, ENGINE_MESSAGES.get(engine, {}).get(name, generic)


def format_findings(file_path, names, engine=None, tenancy=None, notes=()):
    tags = [ENGINE_LABEL[engine]] if engine else []
    if tenancy:
        tags.append(f"multi-tenant: {tenancy['lib']}")
    header = f"Migration Safety Check: {file_path}" + (f" ({', '.join(tags)})" if tags else "")
    lines = [header, ""]
    for name in names:
        level, message = message_for(name, engine)
        lines += [f"[{level}] {name}", message, ""]
    if notes:
        lines.append("Context:")
        for note in notes:
            lines += [note, ""]
    return "\n".join(lines).rstrip()


def main():
    if os.environ.get("DISABLE_MIGRATION_GUARD", "0") == "1":
        return 0
    try:
        data = json.loads(sys.stdin.read())
    except ValueError:
        return 0
    if not isinstance(data, dict):
        return 0

    tool_name = data.get("tool_name", "")
    tool_input = data.get("tool_input")
    if tool_name not in ("Edit", "Write", "MultiEdit") or not isinstance(tool_input, dict):
        return 0

    file_path = tool_input.get("file_path") or ""
    fw = framework_for(file_path) if isinstance(file_path, str) else None
    if not fw:
        return 0
    content = extract_content(tool_name, tool_input)
    if not content:
        return 0

    cwd = data.get("cwd") if isinstance(data.get("cwd"), str) else ""
    files = project_files(file_path, cwd)
    engine = detect_engine(files)
    tenancy = detect_tenancy(files, file_path)

    session_id = data.get("session_id") or "default"
    shown = load_shown(session_id)
    names = [n for n in check_content(content, fw, engine, tenancy) if f"{file_path}:{n}" not in shown]
    if not names:
        return 0
    shown.update(f"{file_path}:{n}" for n in names)
    save_shown(session_id, shown)

    notes = context_notes(names, fw, engine, tenancy, strip_comments(content, fw))
    text = format_findings(file_path, names, engine, tenancy, notes)
    if any(MESSAGES[n][0] == DANGEROUS for n in names):
        print(text, file=sys.stderr)
        return 2
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
