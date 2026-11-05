import json

import pytest

from test_migration_safety_hook import assert_blocked, assert_silent, assert_warned, run_hook, warning_context

RAILS_REL = "db/migrate/20260101000000_x.rb"
DJANGO_REL = "shop/migrations/0002_x.py"
LARAVEL_REL = "database/migrations/2026_01_01_000000_x.php"
PRISMA_REL = "prisma/migrations/20260101000000_x/migration.sql"
KNEX_REL = "migrations/20260101000000_x.js"

PG_YML = "production:\n  adapter: postgresql\n"
MYSQL_LOCK = "    mysql2 (0.5.6)\n"
PRISMA_MYSQL = {"prisma/migrations/migration_lock.toml": 'provider = "mysql"\n'}
PRISMA_PG = {"prisma/migrations/migration_lock.toml": 'provider = "postgresql"\n'}


def project(tmp_path, files):
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    for rel, text in files.items():
        f = root / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    return root


def hook(tmp_path, files, rel, content, **kw):
    root = project(tmp_path, files)
    return run_hook(tmp_path, str(root / rel), content, **kw)


# Engine detection

@pytest.mark.parametrize("files,rel,content,label", [
    ({"config/database.yml": PG_YML}, RAILS_REL, "add_index :t, :c", "PostgreSQL"),
    ({"Gemfile.lock": MYSQL_LOCK}, RAILS_REL, "add_index :t, :c", "MySQL"),
    ({"Gemfile.lock": "    pg (1.5.4)\n"}, RAILS_REL, "add_index :t, :c", "PostgreSQL"),
    ({"requirements.txt": "Django==5.0\npsycopg2-binary==2.9\n"}, DJANGO_REL,
     "migrations.AddIndex(model_name='t', index=i)", "PostgreSQL"),
    ({"pyproject.toml": 'dependencies = [\n  "mysqlclient>=2",\n]\n'}, DJANGO_REL,
     "migrations.AddIndex(model_name='t', index=i)", "MySQL"),
    ({"config/database.php": "'default' => env('DB_CONNECTION', 'mysql'),"}, LARAVEL_REL,
     "Schema::table('t', function ($t) {\n  $t->string('c')->nullable(false);\n});", "MySQL"),
    (PRISMA_MYSQL, PRISMA_REL, 'CREATE INDEX i ON t(c);', "MySQL"),
    ({"prisma/schema.prisma": 'generator c {\n provider = "prisma-client-js"\n}\n'
                              'datasource db {\n provider = "postgresql"\n url = env("DATABASE_URL")\n}\n'},
     PRISMA_REL, 'CREATE INDEX i ON t(c);', "PostgreSQL"),
    ({"package.json": json.dumps({"dependencies": {"knex": "3", "pg": "8"}})}, KNEX_REL,
     "CREATE INDEX i ON t(c)", "PostgreSQL"),
])
def test_engine_is_detected_and_shown(tmp_path, files, rel, content, label):
    ctx = warning_context(hook(tmp_path, files, rel, content))
    assert ctx.splitlines()[0].endswith(f"({label})")


def test_explicit_config_beats_dependencies(tmp_path):
    files = {"config/database.yml": PG_YML, "Gemfile.lock": MYSQL_LOCK}
    assert "(PostgreSQL)" in warning_context(hook(tmp_path, files, RAILS_REL, "add_index :t, :c"))


@pytest.mark.parametrize("files", [
    {},
    {"config/database.yml": "a:\n  adapter: postgresql\nb:\n  adapter: mysql2\n"},
    {"Gemfile.lock": "    pg (1)\n    mysql2 (1)\n"},
])
def test_unknown_or_ambiguous_engine_uses_generic_text(tmp_path, files):
    ctx = warning_context(hook(tmp_path, files, RAILS_REL, "add_index :t, :c"))
    assert ctx.splitlines()[0].endswith("_x.rb")
    assert "Context:" not in ctx


def test_relative_path_resolved_against_cwd(tmp_path):
    root = project(tmp_path, {"config/database.yml": PG_YML})
    assert "(PostgreSQL)" in warning_context(run_hook(tmp_path, RAILS_REL, "add_index :t, :c", cwd=root))


def test_walk_stops_at_git_root(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "database.yml").write_text(PG_YML)
    ctx = warning_context(hook(tmp_path, {}, RAILS_REL, "add_index :t, :c"))
    assert "PostgreSQL" not in ctx


# PostgreSQL-specific rules and text

PG_FILES = {"config/database.yml": PG_YML}


@pytest.mark.parametrize("rel,files,content,name", [
    (PRISMA_REL, PRISMA_PG, 'ALTER TABLE "a" ADD CONSTRAINT "f" FOREIGN KEY ("b") REFERENCES "c"("id");',
     "fk_validation"),
    (PRISMA_REL, PRISMA_PG, 'ALTER TABLE a ADD COLUMN b INT REFERENCES c(id);', "fk_validation"),
    (PRISMA_REL, PRISMA_PG, "ALTER TABLE a ADD CONSTRAINT k CHECK (n > 0);", "check_validation"),
    (PRISMA_REL, PRISMA_PG, "ALTER TABLE a ALTER COLUMN b SET NOT NULL;", "set_not_null"),
    (RAILS_REL, PG_FILES, "add_foreign_key :orders, :users", "fk_validation"),
    (RAILS_REL, PG_FILES, "add_reference :orders, :user, foreign_key: true, index: false", "fk_validation"),
    (RAILS_REL, PG_FILES, 'add_check_constraint :t, "n > 0", name: "k"', "check_validation"),
    (RAILS_REL, PG_FILES, "change_column_null :t, :c, false", "set_not_null"),
    (DJANGO_REL, {}, "migrations.AddField(model_name='o', name='u', "
                     "field=models.ForeignKey(null=True, on_delete=models.CASCADE, to='a.user'))",
     "fk_validation"),
    (DJANGO_REL, {}, "migrations.AddConstraint(model_name='t', "
                     "constraint=models.CheckConstraint(check=Q(n__gt=0), name='k'))", "check_validation"),
    (LARAVEL_REL, {}, "Schema::table('o', function ($t) {\n  $t->foreignId('user_id')->constrained();\n});",
     "fk_validation"),
])
def test_constraint_rules_warn(tmp_path, rel, files, content, name):
    assert_warned(hook(tmp_path, files, rel, content), name)


@pytest.mark.parametrize("rel,content", [
    (PRISMA_REL, 'ALTER TABLE "a" ADD CONSTRAINT "f" FOREIGN KEY ("b") REFERENCES "c"("id") NOT VALID;'),
    (PRISMA_REL, 'ALTER TABLE "a" VALIDATE CONSTRAINT "f";'),
    (PRISMA_REL, "ALTER TABLE a ADD CONSTRAINT k CHECK (n > 0) NOT VALID;"),
    (PRISMA_REL, "CREATE TABLE a (id INT, b INT REFERENCES c(id), CHECK (b > 0));"),
    (RAILS_REL, "add_foreign_key :orders, :users, validate: false"),
    (RAILS_REL, "validate_foreign_key :orders, :users"),
    (RAILS_REL, "add_reference :orders, :user, index: false"),
    (RAILS_REL, 'add_check_constraint :t, "n > 0", name: "k", validate: false'),
    (RAILS_REL, "change_column_null :t, :c, true"),
    (RAILS_REL, "create_table :o do |t|\n  t.references :user, foreign_key: true\nend"),
    (DJANGO_REL, "migrations.AddField(model_name='o', name='u', "
                 "field=models.ForeignKey(null=True, db_constraint=False, on_delete=models.CASCADE, to='a.u'))"),
    (LARAVEL_REL, "Schema::create('o', function ($t) {\n  $t->foreignId('user_id')->constrained();\n});"),
])
def test_safe_constraint_patterns_are_silent(tmp_path, rel, content):
    assert_silent(hook(tmp_path, PRISMA_PG if rel == PRISMA_REL else PG_FILES, rel, content))


def test_postgres_text_and_lock_timeout_note(tmp_path):
    ctx = warning_context(hook(tmp_path, PG_FILES, RAILS_REL, "add_foreign_key :orders, :users"))
    assert "SHARE ROW EXCLUSIVE" in ctx
    assert "validate_foreign_key" in ctx
    assert "lock_timeout" in ctx


def test_lock_timeout_note_skipped_when_already_set(tmp_path):
    content = "execute \"SET lock_timeout = '5s'\"\nadd_foreign_key :orders, :users"
    assert "Context:" not in warning_context(hook(tmp_path, PG_FILES, RAILS_REL, content))


def test_set_not_null_postgres_pattern(tmp_path):
    ctx = warning_context(hook(tmp_path, PRISMA_PG, PRISMA_REL, "ALTER TABLE a ALTER COLUMN b SET NOT NULL;"))
    assert "IS NOT NULL) NOT VALID" in ctx


def test_django_postgres_index_mentions_atomic(tmp_path):
    files = {"requirements.txt": "psycopg[binary]==3.1\n"}
    ctx = warning_context(hook(tmp_path, files, DJANGO_REL, "migrations.AddIndex(model_name='t', index=i)"))
    assert "atomic = False" in ctx


def test_copy_algorithm_ignored_on_postgres(tmp_path):
    assert_silent(hook(tmp_path, PG_FILES, RAILS_REL, "add_column :t, :c, :string, algorithm: :copy"))


# MySQL-specific rules and text

MYSQL_FILES = {"Gemfile.lock": MYSQL_LOCK}


def test_mysql_index_text(tmp_path):
    ctx = warning_context(hook(tmp_path, MYSQL_FILES, RAILS_REL, "add_index :t, :c"))
    assert "[WARNING] non_concurrent_index" in ctx
    assert "ALGORITHM=INPLACE, LOCK=NONE" in ctx
    assert "not transactional" in ctx
    assert "CONCURRENTLY blocks" not in ctx


@pytest.mark.parametrize("rel,files,content", [
    (RAILS_REL, MYSQL_FILES, "add_index :t, :c, algorithm: :inplace"),
    (RAILS_REL, MYSQL_FILES, "add_index :t, :c, algorithm: :instant"),
    (PRISMA_REL, PRISMA_MYSQL, "CREATE INDEX i ON t(c) ALGORITHM=INPLACE LOCK=NONE;"),
    (PRISMA_REL, PRISMA_MYSQL, "ALTER TABLE t ADD INDEX i (c), ALGORITHM=INPLACE, LOCK=NONE;"),
])
def test_mysql_online_index_is_silent(tmp_path, rel, files, content):
    assert_silent(hook(tmp_path, files, rel, content))


@pytest.mark.parametrize("rel,files,content", [
    (RAILS_REL, MYSQL_FILES, "add_index :t, :c, algorithm: :concurrently"),
    (PRISMA_REL, PRISMA_MYSQL, "CREATE INDEX CONCURRENTLY i ON t(c);"),
    (DJANGO_REL, {"requirements.txt": "PyMySQL\n"}, "AddIndexConcurrently(model_name='t', index=i)"),
])
def test_concurrently_flagged_on_mysql(tmp_path, rel, files, content):
    result = hook(tmp_path, files, rel, content)
    assert_warned(result, "concurrently_on_mysql")
    assert "non_concurrent_index" not in warning_context(result)


def test_mysql_add_index_clause_flagged(tmp_path):
    assert_warned(hook(tmp_path, PRISMA_MYSQL, PRISMA_REL, "ALTER TABLE t ADD INDEX i (c);"),
                  "non_concurrent_index")


@pytest.mark.parametrize("content", [
    "ALTER TABLE t ADD COLUMN c INT, ALGORITHM=COPY;",
    "ALTER TABLE t ADD COLUMN c INT, LOCK=EXCLUSIVE;",
    "ALTER TABLE t ADD COLUMN c INT, LOCK = SHARED;",
])
def test_mysql_blocking_algorithm(tmp_path, content):
    assert_warned(hook(tmp_path, PRISMA_MYSQL, PRISMA_REL, content), "mysql_blocking_algorithm")


def test_mysql_not_null_implicit_default_text(tmp_path):
    ctx = warning_context(hook(tmp_path, PRISMA_MYSQL, PRISMA_REL, "ALTER TABLE t ADD COLUMN c INT NOT NULL;"))
    assert "implicit default" in ctx


def test_mysql_fk_text_mentions_gh_ost_limit(tmp_path):
    ctx = warning_context(hook(tmp_path, MYSQL_FILES, RAILS_REL, "add_foreign_key :orders, :users"))
    assert "gh-ost does not support foreign keys" in ctx


# Multi-tenancy

@pytest.mark.parametrize("files,rel,content,lib,command", [
    ({"Gemfile.lock": "    ros-apartment (2.11.0)\n"}, RAILS_REL, "add_index :t, :c",
     "ros-apartment", "rake apartment:migrate"),
    ({"Gemfile.lock": "    apartment (2.2.1)\n"}, RAILS_REL, "add_index :t, :c",
     "apartment", "rake apartment:migrate"),
    ({"requirements.txt": "django-tenants==3.6\n"}, DJANGO_REL,
     "migrations.AddIndex(model_name='t', index=i)", "django-tenants", "migrate_schemas"),
    ({"composer.json": json.dumps({"require": {"stancl/tenancy": "^3.8"}})}, LARAVEL_REL,
     "Schema::table('t', function ($t) {\n  $t->unique('e');\n});", "stancl/tenancy", "tenants:migrate"),
    ({"composer.json": json.dumps({"require": {"spatie/laravel-multitenancy": "^3"}})}, LARAVEL_REL,
     "Schema::table('t', function ($t) {\n  $t->unique('e');\n});", "spatie/laravel-multitenancy", None),
    ({}, "database/migrations/tenant/2026_01_01_x.php",
     "Schema::table('t', function ($t) {\n  $t->unique('e');\n});", "tenant migrations folder", None),
])
def test_schema_per_tenant_note(tmp_path, files, rel, content, lib, command):
    ctx = warning_context(hook(tmp_path, files, rel, content))
    assert f"multi-tenant: {lib}" in ctx.splitlines()[0]
    assert f"Multi-tenant ({lib})" in ctx
    assert "once per tenant" in ctx
    if command:
        assert command in ctx
    else:
        assert "Run with:" not in ctx


def test_tenancy_note_rides_along_with_blocks(tmp_path):
    result = hook(tmp_path, {"Gemfile.lock": "    ros-apartment (2)\n"}, RAILS_REL, "remove_column :t, :c")
    assert_blocked(result, "drop_column")
    assert "once per tenant" in result.stderr


def test_tenancy_alone_is_silent(tmp_path):
    files = {"Gemfile.lock": "    ros-apartment (2)\n    pg (1)\n"}
    assert_silent(hook(tmp_path, files, RAILS_REL, "add_column :t, :c, :string"))


ROW = {"Gemfile.lock": "    acts_as_tenant (1.0)\n"}


@pytest.mark.parametrize("rel,content", [
    (RAILS_REL, "add_index :users, :email, unique: true, algorithm: :concurrently"),
    (PRISMA_REL, 'CREATE UNIQUE INDEX CONCURRENTLY "u" ON "users"("email");'),
    (DJANGO_REL, "migrations.AddField(model_name='u', name='e', field=models.CharField(unique=True, null=True))"),
    (LARAVEL_REL, "Schema::table('u', function ($t) {\n  $t->unique('email');\n});"),
])
def test_row_tenancy_unique_without_tenant(tmp_path, rel, content):
    assert_warned(hook(tmp_path, ROW, rel, content), "unique_without_tenant")


@pytest.mark.parametrize("rel,content", [
    (RAILS_REL, "add_index :users, [:account_id, :email], unique: true, algorithm: :concurrently"),
    (PRISMA_REL, 'CREATE UNIQUE INDEX CONCURRENTLY "u" ON "users"("tenant_id", "email");'),
])
def test_row_tenancy_unique_with_tenant_column(tmp_path, rel, content):
    ctx = warning_context(hook(tmp_path, ROW, rel, content))
    assert "unique_constraint" in ctx
    assert "[WARNING] unique_without_tenant" not in ctx
    assert "once per tenant" not in ctx


def test_unique_without_tenant_needs_row_tenancy(tmp_path):
    ctx = warning_context(hook(tmp_path, {}, RAILS_REL, "add_index :u, :e, unique: true, algorithm: :concurrently"))
    assert "[WARNING] unique_without_tenant" not in ctx


# SENTINEL_DATABASE rehearsal: credentials are never read

@pytest.mark.parametrize("rel,content,command", [
    (RAILS_REL, "remove_column :t, :c", 'DATABASE_URL="$SENTINEL_DATABASE" bin/rails db:migrate'),
    (DJANGO_REL, "migrations.RemoveField(model_name='t', name='c')", "python manage.py migrate"),
    (LARAVEL_REL, "Schema::table('t', function ($t) {\n  $t->dropColumn('c');\n});",
     'DB_URL="$SENTINEL_DATABASE" php artisan migrate'),
    (PRISMA_REL, 'ALTER TABLE "t" DROP COLUMN "c";', "npx prisma migrate deploy"),
    (KNEX_REL, "table.dropColumn('c')", "npx knex migrate:latest"),
])
def test_dangerous_gives_rehearsal_command(tmp_path, rel, content, command):
    result = hook(tmp_path, {}, rel, content)
    assert result.returncode == 2
    assert "never production" in result.stderr
    assert command in result.stderr


def test_database_urls_are_never_read_or_printed(tmp_path):
    secret = "postgres://app:s3cret@prod:5432/db"
    files = {".env": f"DATABASE_URL={secret}\nSENTINEL_DATABASE={secret}\nDB_CONNECTION=pgsql\n"}
    env = {"SENTINEL_DATABASE": secret, "DATABASE_URL": secret, "DB_URL": secret}
    result = hook(tmp_path, files, LARAVEL_REL,
                  "Schema::table('t', function ($t) {\n  $t->dropColumn('c');\n});", env=env)
    assert result.returncode == 2
    assert "s3cret" not in result.stderr + result.stdout
    assert "prod:5432" not in result.stderr + result.stdout
    assert "PostgreSQL" not in result.stderr


def test_no_rehearsal_note_for_warnings_only(tmp_path):
    ctx = warning_context(hook(tmp_path, {}, RAILS_REL, "add_index :t, :c"))
    assert "SENTINEL" not in ctx
    assert "Rehearse" not in ctx


def test_ruby_references_inside_alter_file_is_not_sql_fk(tmp_path):
    content = ('execute "ALTER TABLE t ADD COLUMN x INT"\n'
               "create_table :o do |t|\n  t.references :user\nend")
    assert_silent(hook(tmp_path, PG_FILES, RAILS_REL, content))


def test_rails_execute_raw_fk_is_flagged(tmp_path):
    content = 'execute "ALTER TABLE o ADD CONSTRAINT f FOREIGN KEY (u) REFERENCES users(id)"'
    assert_warned(hook(tmp_path, PG_FILES, RAILS_REL, content), "fk_validation")


def test_django_unique_constraint_is_flagged(tmp_path):
    content = ("migrations.AddConstraint(model_name='u', "
               "constraint=models.UniqueConstraint(fields=['email'], name='u_email'))")
    assert_warned(hook(tmp_path, {}, DJANGO_REL, content), "unique_constraint")


def test_mysql_concurrently_does_not_hide_other_plain_rails_index(tmp_path):
    content = "add_index :t, :a, algorithm: :concurrently\nadd_index :t, :b"
    ctx = warning_context(hook(tmp_path, MYSQL_FILES, RAILS_REL, content))
    assert "[WARNING] concurrently_on_mysql" in ctx
    assert "[WARNING] non_concurrent_index" in ctx


def test_walk_stops_at_cwd(tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "database.yml").write_text(PG_YML)
    root = tmp_path / "proj"
    (root / "db" / "migrate").mkdir(parents=True)
    ctx = warning_context(run_hook(tmp_path, str(root / RAILS_REL), "add_index :t, :c", cwd=root))
    assert "PostgreSQL" not in ctx
