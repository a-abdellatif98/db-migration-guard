import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / "scripts" / "migration_safety_hook.py"

RAILS = "/app/db/migrate/20260101000000_change_things.rb"
DJANGO = "/app/shop/migrations/0002_change_things.py"
LARAVEL = "/app/database/migrations/2026_01_01_000000_change_things.php"
PRISMA = "/app/prisma/migrations/20260101000000_change_things/migration.sql"
KNEX = "/app/migrations/20260101000000_change_things.js"


def run_hook(home, path=RAILS, content="", tool="Write", session="s1", payload=None, env=None, cwd=None):
    if payload is None:
        tool_input = {"file_path": path}
        if tool == "Write":
            tool_input["content"] = content
        elif tool == "Edit":
            tool_input.update(old_string="x", new_string=content)
        elif tool == "MultiEdit":
            tool_input["edits"] = [
                {"old_string": "a", "new_string": "harmless"},
                {"old_string": "b", "new_string": content},
            ]
        payload = {"session_id": session, "tool_name": tool, "tool_input": tool_input}
        if cwd:
            payload["cwd"] = str(cwd)
    full_env = {**os.environ, "HOME": str(home), "TMPDIR": str(home)}
    for var in ("DISABLE_MIGRATION_GUARD", "SENTINEL_DATABASE", "DATABASE_URL", "DB_URL"):
        full_env.pop(var, None)
    full_env.update(env or {})
    stdin = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        [sys.executable, str(HOOK)], input=stdin, capture_output=True, text=True, env=full_env
    )


def warning_context(result):
    return json.loads(result.stdout)["hookSpecificOutput"]["additionalContext"]


def assert_silent(result):
    assert result.returncode == 0, result.stderr
    assert result.stdout == ""
    assert result.stderr == ""


def assert_blocked(result, name):
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert f"[DANGEROUS] {name}" in result.stderr


def assert_warned(result, name):
    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert f"[WARNING] {name}" in out["additionalContext"]


SAFE = [
    ("rails_concurrent_index", RAILS,
     "disable_ddl_transaction!\nadd_index :t, :c, algorithm: :concurrently"),
    ("django_add_index_concurrently", DJANGO,
     "operations = [AddIndexConcurrently(model_name='t', index=models.Index(fields=['c'], name='i'))]"),
    ("prisma_add_nullable_column", PRISMA, 'ALTER TABLE "x" ADD COLUMN "y" INTEGER;'),
    ("sql_create_table_not_null", PRISMA, "CREATE TABLE t (id SERIAL NOT NULL, name TEXT NOT NULL)"),
    ("sql_create_index_concurrently", PRISMA, 'CREATE INDEX CONCURRENTLY "i" ON "t"("c");'),
    ("sql_create_unique_in_create_table", PRISMA, "CREATE TABLE t (id INT NOT NULL, email TEXT UNIQUE)"),
    ("rails_create_table_null_false", RAILS,
     "create_table :t do |t|\n  t.string :name, null: false\n  t.index :name, unique: true\nend"),
    ("rails_add_column_nullable", RAILS, "add_column :t, :c, :string"),
    ("rails_add_column_not_null_with_default", RAILS,
     "add_column :t, :c, :string, null: false, default: ''"),
    ("rails_add_column_multiline_default", RAILS,
     "add_column :t, :c, :string,\n  null: false,\n  default: ''"),
    ("rails_change_column_default", RAILS, "change_column_default :t, :c, from: nil, to: 1"),
    ("rails_down_drop_table", RAILS,
     "def up\n  create_table :t do |t|\n    t.string :n\n  end\nend\n\ndef down\n  drop_table :t\nend"),
    ("rails_commented_remove_column", RAILS, "# remove_column :t, :c\nadd_column :t, :d, :string"),
    ("django_create_model", DJANGO,
     "migrations.CreateModel(name='T', fields=[('email', models.CharField(max_length=9, unique=True, null=False))])"),
    ("django_add_field_not_null_default", DJANGO,
     "migrations.AddField(model_name='t', name='c', field=models.IntegerField(null=False, default=0))"),
    ("django_add_nullable_field", DJANGO,
     "migrations.AddField(model_name='t', name='c', field=models.IntegerField(null=True))"),
    ("django_run_python_only", DJANGO, "operations = [migrations.RunPython(forwards)]"),
    ("laravel_create_table", LARAVEL,
     "Schema::create('t', function (Blueprint $table) {\n"
     "    $table->string('email')->unique();\n"
     "    $table->string('name')->nullable(false);\n"
     "});\n"
     "public function down()\n{\n    Schema::dropIfExists('t');\n}"),
    ("laravel_add_nullable", LARAVEL,
     "Schema::table('t', function (Blueprint $table) {\n    $table->string('c')->nullable();\n});"),
    ("knex_create_table", KNEX, "knex.schema.createTable('t', t => { t.string('a').notNullable(); })"),
    ("non_migration_file", "/app/app/models/user.rb", "remove_column :t, :c\nDROP TABLE t"),
    ("rails_non_ruby_file", "/app/db/migrate/README.md", "remove_column :t, :c"),
    ("django_init_file", "/app/shop/migrations/__init__.py", "RemoveField"),
    ("python_text_in_rails_syntax_ignored_for_prisma", PRISMA, "-- remove_column AddIndex"),
]


@pytest.mark.parametrize("path,content", [c[1:] for c in SAFE], ids=[c[0] for c in SAFE])
def test_safe_migrations_are_not_flagged(tmp_path, path, content):
    assert_silent(run_hook(tmp_path, path, content))


BLOCKED = [
    ("rails_remove_column", RAILS, "remove_column :users, :email, :string", "drop_column"),
    ("django_remove_field", DJANGO, "migrations.RemoveField(model_name='u', name='e')", "drop_column"),
    ("laravel_drop_column", LARAVEL,
     "Schema::table('u', function (Blueprint $t) {\n  $t->dropColumn('e');\n});", "drop_column"),
    ("sql_drop_column", PRISMA, 'ALTER TABLE "u" DROP COLUMN "e";', "drop_column"),
    ("knex_drop_column", KNEX, "table.dropColumn('e')", "drop_column"),
    ("rails_drop_table", RAILS, "def change\n  drop_table :users\nend", "drop_table"),
    ("django_delete_model", DJANGO, "migrations.DeleteModel(name='User')", "drop_table"),
    ("laravel_drop_table", LARAVEL,
     "public function up()\n{\n    Schema::dropIfExists('users');\n}", "drop_table"),
    ("sql_drop_table", PRISMA, 'DROP TABLE "users";', "drop_table"),
    ("rails_rename_column", RAILS, "rename_column :u, :a, :b", "rename_column"),
    ("rails_rename_table", RAILS, "rename_table :u, :v", "rename_column"),
    ("django_rename_field", DJANGO, "migrations.RenameField('u', 'a', 'b')", "rename_column"),
    ("django_rename_model", DJANGO, "migrations.RenameModel('A', 'B')", "rename_column"),
    ("laravel_rename_column", LARAVEL, "$table->renameColumn('a', 'b');", "rename_column"),
    ("knex_rename_column", KNEX, "table.renameColumn('a', 'b')", "rename_column"),
    ("sql_rename_column", PRISMA, 'ALTER TABLE "u" RENAME COLUMN "a" TO "b";', "rename_column"),
    ("sql_rename_to", PRISMA, 'ALTER TABLE "u" RENAME TO "v";', "rename_column"),
    ("rails_change_column", RAILS, "change_column :t, :c, :text", "change_type"),
    ("django_alter_field", DJANGO,
     "migrations.AlterField(model_name='t', name='c', field=models.TextField())", "change_type"),
    ("laravel_change", LARAVEL,
     "Schema::table('t', function ($t) {\n  $t->text('c')->change();\n});", "change_type"),
    ("sql_alter_column_type", PRISMA, 'ALTER TABLE "t" ALTER COLUMN "c" TYPE TEXT;', "change_type"),
    ("sql_alter_column_set_data_type", PRISMA,
     'ALTER TABLE "t" ALTER COLUMN "c" SET DATA TYPE TEXT;', "change_type"),
    ("rails_remove_index", RAILS, "remove_index :t, :c", "remove_index"),
    ("django_remove_index", DJANGO, "migrations.RemoveIndex(model_name='t', name='i')", "remove_index"),
    ("laravel_drop_index", LARAVEL, "$table->dropIndex('i');", "remove_index"),
    ("sql_drop_index", PRISMA, 'DROP INDEX "i";', "remove_index"),
    ("windows_path_rails", r"C:\app\db\migrate\20240101_rm.rb",
     "remove_column :users, :email, :string", "drop_column"),
    ("windows_path_prisma", r"C:\app\prisma\migrations\2\migration.sql",
     'ALTER TABLE "u" DROP COLUMN "e";', "drop_column"),
]


@pytest.mark.parametrize("path,content,name", [c[1:] for c in BLOCKED], ids=[c[0] for c in BLOCKED])
def test_dangerous_patterns_block(tmp_path, path, content, name):
    assert_blocked(run_hook(tmp_path, path, content), name)


WARNED = [
    ("rails_not_null_no_default", RAILS, "add_column :t, :c, :string, null: false", "not_null_no_default"),
    ("rails_not_null_multiline", RAILS,
     "add_column :t, :c, :string,\n  null: false", "not_null_no_default"),
    ("django_not_null_no_default", DJANGO,
     "migrations.AddField(model_name='t', name='c', field=models.IntegerField(null=False))",
     "not_null_no_default"),
    ("laravel_not_null_no_default", LARAVEL,
     "Schema::table('t', function ($t) {\n  $t->string('c')->nullable(false);\n});",
     "not_null_no_default"),
    ("sql_add_column_not_null", PRISMA,
     'ALTER TABLE "t" ADD COLUMN "c" TEXT NOT NULL;', "not_null_no_default"),
    ("sql_add_column_not_null_multi_clause", PRISMA,
     'ALTER TABLE "t" ADD COLUMN "a" INT DEFAULT 0 NOT NULL, ADD COLUMN "c" TEXT NOT NULL;',
     "not_null_no_default"),
    ("rails_add_index", RAILS, "add_index :orders, :customer_id", "non_concurrent_index"),
    ("django_add_index", DJANGO,
     "migrations.AddIndex(model_name='t', index=models.Index(fields=['c'], name='i'))",
     "non_concurrent_index"),
    ("sql_create_index", PRISMA, 'CREATE INDEX "i" ON "t"("c");', "non_concurrent_index"),
    ("rails_data_in_schema", RAILS,
     "add_column :t, :c, :string\nUser.update_all(c: 'x')", "data_in_schema_migration"),
    ("rails_execute_update_in_schema", RAILS,
     "add_column :t, :c, :string\nexecute \"UPDATE t SET c = 'x'\"", "data_in_schema_migration"),
    ("django_run_python_with_schema", DJANGO,
     "operations = [migrations.AddField(model_name='t', name='c', "
     "field=models.IntegerField(null=True)), migrations.RunPython(fill)]",
     "data_in_schema_migration"),
    ("laravel_db_update_with_schema", LARAVEL,
     "Schema::table('t', function ($t) {\n  $t->string('c')->nullable();\n});\n"
     "DB::table('t')->update(['c' => 'x']);", "data_in_schema_migration"),
    ("rails_unique_index", RAILS,
     "add_index :users, :email, unique: true, algorithm: :concurrently", "unique_constraint"),
    ("django_unique_alter", DJANGO,
     "migrations.AddField(model_name='t', name='c', field=models.CharField(max_length=9, unique=True, null=True))",
     "unique_constraint"),
    ("laravel_unique", LARAVEL,
     "Schema::table('t', function ($t) {\n  $t->unique('email');\n});", "unique_constraint"),
    ("sql_add_constraint_unique", PRISMA,
     'ALTER TABLE "t" ADD CONSTRAINT "u" UNIQUE ("c");', "unique_constraint"),
    ("sql_create_unique_index", PRISMA,
     'CREATE UNIQUE INDEX CONCURRENTLY "u" ON "t"("c");', "unique_constraint"),
]


@pytest.mark.parametrize("path,content,name", [c[1:] for c in WARNED], ids=[c[0] for c in WARNED])
def test_warning_patterns_do_not_block_but_surface_message(tmp_path, path, content, name):
    assert_warned(run_hook(tmp_path, path, content), name)


def test_non_concurrent_index_exact_claim_from_review(tmp_path):
    result = run_hook(tmp_path, RAILS, "add_index :t, :c, algorithm: :concurrently")
    assert_silent(result)


def test_edit_and_multiedit_shapes(tmp_path):
    assert_blocked(run_hook(tmp_path, RAILS, "remove_column :t, :c", tool="Edit"), "drop_column")
    assert_blocked(
        run_hook(tmp_path, RAILS, "remove_column :t, :c", tool="MultiEdit", session="s2"), "drop_column"
    )
    assert_silent(run_hook(tmp_path, RAILS, "add_column :t, :c, :string", tool="MultiEdit", session="s3"))


def test_mixed_findings_block_and_include_warning_text(tmp_path):
    result = run_hook(tmp_path, RAILS, "remove_column :t, :c\nadd_index :t, :d")
    assert result.returncode == 2
    assert "[DANGEROUS] drop_column" in result.stderr
    assert "[WARNING] non_concurrent_index" in result.stderr


def test_same_finding_is_shown_once_per_session(tmp_path):
    first = run_hook(tmp_path, RAILS, "add_index :t, :c")
    second = run_hook(tmp_path, RAILS, "add_index :t, :c")
    assert_warned(first, "non_concurrent_index")
    assert_silent(second)
    assert_warned(run_hook(tmp_path, RAILS, "add_index :t, :c", session="other"), "non_concurrent_index")


@pytest.mark.parametrize(
    "payload", ["not json", "[]", "null", json.dumps({"tool_name": "Write"}),
                json.dumps({"tool_name": "Bash", "tool_input": {"command": "ls"}}),
                json.dumps({"tool_name": "Write", "tool_input": {"file_path": RAILS}}),
                json.dumps({"tool_name": "Write", "tool_input": None})],
)
def test_malformed_or_irrelevant_input_exits_zero(tmp_path, payload):
    assert_silent(run_hook(tmp_path, payload=payload))


def test_disable_env_var(tmp_path):
    result = run_hook(tmp_path, RAILS, "remove_column :t, :c", env={"DISABLE_MIGRATION_GUARD": "1"})
    assert_silent(result)


def test_state_files_are_written_under_home_and_old_ones_pruned(tmp_path):
    claude = tmp_path / "db-migration-guard"
    claude.mkdir()
    old = claude / "migration_guard_state_old.json"
    fresh = claude / "migration_guard_state_fresh.json"
    unrelated = claude / "settings.json"
    for f in (old, fresh, unrelated):
        f.write_text("[]")
    eight_days_ago = time.time() - 8 * 86400
    os.utime(old, (eight_days_ago, eight_days_ago))
    os.utime(unrelated, (eight_days_ago, eight_days_ago))

    run_hook(tmp_path, RAILS, "add_index :t, :c", session="cur")

    assert not old.exists()
    assert fresh.exists()
    assert unrelated.exists()
    assert (claude / "migration_guard_state_cur.json").exists()


def test_hostile_session_id_stays_inside_claude_dir(tmp_path):
    run_hook(tmp_path, RAILS, "add_index :t, :c", session="../../evil")
    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files and all(p.parent == tmp_path / "db-migration-guard" for p in files)


def test_never_touches_claude_config_dir(tmp_path):
    run_hook(tmp_path, RAILS, "add_index :t, :c")
    assert not (tmp_path / ".claude").exists()
