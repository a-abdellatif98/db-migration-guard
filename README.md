# db-migration-guard

A plugin that catches dangerous database migration patterns before they reach production.

## What It Does

- Detects destructive operations (dropping columns/tables, renames, type changes)
- Warns about locking risks on large tables (non-concurrent indexes)
- Suggests zero-downtime migration patterns (add/backfill/swap/drop)
- Flags NOT NULL additions without defaults
- Catches mixed data/schema migrations
- Flags unvalidated foreign keys, CHECK constraints and SET NOT NULL scans
- Detects PostgreSQL vs MySQL and gives engine-specific fixes (CONCURRENTLY and NOT VALID vs ALGORITHM=INPLACE and online schema change tools)
- Detects multi-tenant setups (Apartment, django-tenants, stancl/tenancy, spatie, acts_as_tenant)
- Suggests rehearsing DANGEROUS changes on a sentinel database (a restored copy of production)
- Works across Rails, Django, Laravel, Prisma, and Knex

## Installation

```
/plugin install db-migration-guard@claude-plugins-official
```

## Usage

### Automatic Protection (Hook)

A prompt hook checks every migration edit in Claude Code. It blocks DANGEROUS changes (dropping a column or table, renaming, changing a column type, removing an index) and tells Claude the safe pattern. Everything else goes through.

To go ahead with a change you have reviewed, ask Claude to add the comment `migration-guard: confirmed` to the migration (Rails `safety_assured` blocks also pass). The hook is a single model call per Edit/Write, so it adds a short delay to file edits.

### Skill: engine, tenancy and sentinel guidance

The `migrate-check` skill carries the detailed advice, on Claude.ai as well as in Claude Code:

- **PostgreSQL vs MySQL**: detected from Prisma's `migration_lock.toml` or driver dependencies (`Gemfile.lock`, `requirements*.txt`, `pyproject.toml`, `package.json`), never from database config files. PostgreSQL gets `CONCURRENTLY` and `NOT VALID` patterns; MySQL gets `ALGORITHM=INPLACE, LOCK=NONE`, gh-ost and pt-online-schema-change.
- **Multi-tenant projects**:

| Library | Model | Extra advice |
|---|---|---|
| `ros-apartment`, `apartment` | schema per tenant | fan-out, `rake apartment:migrate` |
| `django-tenants`, `django-tenant-schemas` | schema per tenant | fan-out, `migrate_schemas` |
| `stancl/tenancy` | database per tenant | fan-out, `php artisan tenants:migrate` |
| `spatie/laravel-multitenancy` | database per tenant | fan-out |
| `database/migrations/tenant/` folder | per tenant | fan-out |
| `acts_as_tenant` | shared schema | unique indexes must include the tenant column |

- **Sentinel database**: for DANGEROUS changes, advice to rehearse on a restored copy of production set up as a named `sentinel` connection, using the framework's own connection feature. The plugin runs nothing.

### Slash Command

Run a full safety scan on all pending migrations:

```
/migrate-check
/migrate-check db/migrate
/migrate-check prisma/migrations
```

### Skill (Auto-Activated)

The `migrate-check` skill activates automatically when you discuss migration safety, schema changes, or database operations. It provides contextual guidance inline.

### Agent

The `migration-reviewer` agent performs deep analysis - cross-referencing schema files, checking foreign key dependencies, and estimating table sizes to provide risk-aware recommendations.

## Supported Frameworks

| Framework | Migration Path | Schema File |
|---|---|---|
| Rails | `db/migrate/*.rb` | `db/schema.rb` |
| Django | `app/migrations/*.py` | `models.py` |
| Laravel | `database/migrations/*.php` | - |
| Prisma | `prisma/migrations/*.sql` | `schema.prisma` |
| Knex | `migrations/*.js` | - |

## Risk Levels

- **SAFE** - Adding nullable columns, creating new tables, adding indexes concurrently
- **WARNING** (flagged by the skill and `/migrate-check`, never blocks) - NOT NULL columns without a default, blocking index builds, unique constraints, unvalidated foreign keys and CHECKs, SET NOT NULL, MySQL `ALGORITHM=COPY` / `LOCK=EXCLUSIVE`, `CONCURRENTLY` on MySQL, tenant-less unique indexes, data changes mixed into schema migrations
- **DANGEROUS** (blocked by the hook until confirmed) - Dropping columns/tables, renames, column type changes, removing indexes

## Examples

### Dangerous: Dropping a column

```ruby
# This triggers a DANGEROUS warning
class RemoveEmailFromUsers < ActiveRecord::Migration[7.0]
  def change
    remove_column :users, :email, :string
  end
end
```

Suggested safe alternative:

```ruby
# Step 1: Stop writing (app code change, no migration)
# Step 2: Stop reading (app code change, no migration)
# Step 3: Remove the column
class RemoveEmailFromUsers < ActiveRecord::Migration[7.0]
  def change
    safety_assured { remove_column :users, :email, :string }
  end
end
```

### Warning: Non-concurrent index

```ruby
# This triggers a WARNING
class AddIndexToOrders < ActiveRecord::Migration[7.0]
  def change
    add_index :orders, :customer_id
  end
end
```

Suggested fix:

```ruby
class AddIndexToOrders < ActiveRecord::Migration[7.0]
  disable_ddl_transaction!

  def change
    add_index :orders, :customer_id, algorithm: :concurrently
  end
end
```

## Development

```
pip install pytest
python3 -m pytest tests
```

## License

MIT
