# db-migration-guard

A Claude Code plugin that catches dangerous database migration patterns before they reach production.

## What It Does

- Detects destructive operations (dropping columns/tables, renames, type changes)
- Warns about locking risks on large tables (non-concurrent indexes)
- Suggests zero-downtime migration patterns (add/backfill/swap/drop)
- Flags NOT NULL additions without defaults
- Catches mixed data/schema migrations
- Flags unvalidated foreign keys, CHECK constraints and SET NOT NULL scans
- Detects PostgreSQL vs MySQL and gives engine-specific fixes (CONCURRENTLY and NOT VALID vs ALGORITHM=INPLACE and online schema change tools)
- Detects multi-tenant setups (Apartment, django-tenants, stancl/tenancy, spatie, acts_as_tenant)
- Suggests rehearsing DANGEROUS changes on a sentinel database (`SENTINEL_DATABASE`)
- Works across Rails, Django, Laravel, Prisma, and Knex

## Installation

```
/plugin install db-migration-guard@claude-plugins-official
```

## Usage

### Automatic Protection (Hooks)

The plugin automatically checks migration file edits. DANGEROUS patterns block the edit with an explanation; WARNING patterns are passed to Claude as context without blocking. No action needed - it runs whenever you create or edit a migration file.

Disable with the environment variable:

```
DISABLE_MIGRATION_GUARD=1
```

### Database engine detection

The hook walks up from the migration file to the git root and reads `config/database.yml`, `.env` / `config/database.php`, Prisma's `migration_lock.toml` / `schema.prisma`, then driver dependencies (`Gemfile.lock`, `requirements*.txt`, `pyproject.toml`, `package.json`). If it finds exactly one of PostgreSQL or MySQL, warnings use that engine's fix. Otherwise it shows advice for both.

### Multi-tenant projects

| Library | Model | Extra advice |
|---|---|---|
| `ros-apartment`, `apartment` | schema per tenant | fan-out note, `rake apartment:migrate` |
| `django-tenants`, `django-tenant-schemas` | schema per tenant | fan-out note, `migrate_schemas` |
| `stancl/tenancy` | database per tenant | fan-out note, `php artisan tenants:migrate` |
| `spatie/laravel-multitenancy` | database per tenant | fan-out note |
| `database/migrations/tenant/` folder | per tenant | fan-out note |
| `acts_as_tenant` | shared schema | warns when a unique index lacks the tenant column |

### Sentinel database

Point `SENTINEL_DATABASE` at a copy of production (in your environment or `.env`). When a DANGEROUS change is found, the hook adds the exact command to rehearse the migration against it, e.g. `DATABASE_URL="$SENTINEL_DATABASE" bin/rails db:migrate`. The password is masked, and the hook refuses if `SENTINEL_DATABASE` equals `DATABASE_URL`. The hook never runs migrations itself.

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
- **WARNING** (shown to Claude, does not block) - NOT NULL columns without a default, blocking index builds, unique constraints, unvalidated foreign keys and CHECKs, SET NOT NULL, MySQL `ALGORITHM=COPY` / `LOCK=EXCLUSIVE`, `CONCURRENTLY` on MySQL, tenant-less unique indexes, data changes mixed into schema migrations
- **DANGEROUS** (blocks the edit once per file and pattern) - Dropping columns/tables, renames, column type changes, removing indexes

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
