#!/usr/bin/env python3
"""PreToolUse hook: blocks DANGEROUS migration changes, surfaces WARNING ones without blocking."""

import glob
import json
import os
import re
import sys
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
}

RULE_ORDER = list(MESSAGES)

SQL = {
    "drop_column": r"(?i:\bDROP\s+COLUMN\b)",
    "drop_table": r"(?i:\bDROP\s+TABLE\b)",
    "rename_column": r"(?i:\bRENAME\s+(?:COLUMN|TO|TABLE)\b)",
    "change_type": r"(?i:\bALTER\s+COLUMN\s+\S+\s+(?:SET\s+DATA\s+)?TYPE\b)",
    "remove_index": r"(?i:\bDROP\s+INDEX\b)",
    "non_concurrent_index": r"(?i:\bCREATE\s+(?:UNIQUE\s+)?INDEX\b(?!\s+CONCURRENTLY\b))",
    "unique_constraint": r"(?i:\bADD\s+(?:CONSTRAINT\s+\S+\s+)?UNIQUE\b|\bCREATE\s+UNIQUE\s+INDEX\b)",
}

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


def detect(fw, content):
    found = set()
    syntax = SYNTAX[fw]
    for name in RULE_ORDER:
        for rx in (syntax.get(name), SQL.get(name)):
            if rx and re.search(rx, content):
                found.add(name)

    if fw == "rails":
        for stmt in ruby_statements(content):
            if re.search(r"\badd_index\b", stmt):
                if not re.search(r"concurrently", stmt):
                    found.add("non_concurrent_index")
                if re.search(r"\bunique:\s*true\b", stmt):
                    found.add("unique_constraint")
            if (re.search(r"\badd_column\b", stmt) and re.search(r"\bnull:\s*false\b", stmt)
                    and not re.search(r"\bdefault:", stmt)):
                found.add("not_null_no_default")
    elif fw == "django":
        for call in balanced_calls(content, "AddField"):
            if re.search(r"\bnull\s*=\s*False\b", call) and not re.search(r"\bdefault\s*=", call):
                found.add("not_null_no_default")
        for call in balanced_calls(content, "AddField") + balanced_calls(content, "AlterField"):
            if re.search(r"\bunique\s*=\s*True\b", call):
                found.add("unique_constraint")
    elif fw == "laravel":
        for stmt in php_alter_statements(content):
            if re.search(r"->\s*nullable\s*\(\s*false\s*\)", stmt) and not re.search(r"->\s*default\s*\(", stmt):
                found.add("not_null_no_default")
            if re.search(r"->\s*unique\s*\(", stmt):
                found.add("unique_constraint")

    if sql_adds_not_null_column(content, fw):
        found.add("not_null_no_default")

    if fw in DATA_OPS and re.search(DATA_OPS[fw], content) and re.search(SCHEMA_OPS[fw], content):
        found.add("data_in_schema_migration")

    return [name for name in RULE_ORDER if name in found]


def check_content(content, fw):
    return detect(fw, strip_down(strip_comments(content, fw), fw))


def state_dir():
    return os.path.join(os.path.expanduser("~"), ".claude")


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


def format_findings(file_path, names):
    lines = [f"Migration Safety Check: {file_path}", ""]
    for name in names:
        level, message = MESSAGES[name]
        lines += [f"[{level}] {name}", message, ""]
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

    session_id = data.get("session_id") or "default"
    shown = load_shown(session_id)
    names = [n for n in check_content(content, fw) if f"{file_path}:{n}" not in shown]
    if not names:
        return 0
    shown.update(f"{file_path}:{n}" for n in names)
    save_shown(session_id, shown)

    text = format_findings(file_path, names)
    if any(MESSAGES[n][0] == DANGEROUS for n in names):
        print(text, file=sys.stderr)
        return 2
    print(json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": text}}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
