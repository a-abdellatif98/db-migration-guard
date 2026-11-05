# Migration Pattern Catalogue

Per-framework syntax and the safe alternative for each dangerous pattern.

## 1. Dropping Columns or Tables
- `remove_column`, `drop_table` (Rails)
- `RemoveField`, `DeleteModel` (Django)
- `dropColumn`, `drop` (Laravel)
- `DROP COLUMN`, `DROP TABLE` (raw SQL / Prisma)

**Warning**: Data loss is irreversible. Suggest a phased approach:
1. Stop writing to the column in application code
2. Deploy code that no longer reads from the column
3. Add the migration to remove the column
4. Keep a backup or reversible migration ready

## 2. Renaming Columns or Tables
- `rename_column`, `rename_table` (Rails)
- `RenameField`, `RenameModel` (Django)
- `renameColumn`, `rename` (Laravel)
- `RENAME COLUMN`, `ALTER TABLE ... RENAME` (raw SQL)

**Warning**: Renaming causes downtime during deployment if old code references the old name. Suggest the safe pattern:
1. Add the new column
2. Backfill data from old to new
3. Update application code to use new column
4. Drop the old column in a separate migration

## 3. Changing Column Types
- `change_column` (Rails)
- `AlterField` with type change (Django)
- `change()` with type modification (Laravel)
- `ALTER COLUMN ... TYPE` (raw SQL)

**Warning**: Type changes can fail on existing data and may lock the table. Suggest:
- Test with production-like data first
- For large tables, use the add/backfill/swap/drop pattern
- Check that existing data is compatible with the new type

## 4. Adding NOT NULL Without Default
- `add_column ..., null: false` without `default:` (Rails)
- `null=False` without `default=` (Django)
- `->nullable(false)` without `->default()` (Laravel)
- `NOT NULL` without `DEFAULT` (raw SQL)

**Warning**: On PostgreSQL this fails if the table has existing rows. On MySQL it succeeds silently and fills existing rows with the type's implicit default (`''`, `0`), which is usually wrong data. Suggest:
1. Add the column as nullable first
2. Backfill existing rows with the desired value
3. Add the NOT NULL constraint in a separate migration

## 5. Index Creation That Blocks Writes
- `add_index` without `algorithm: :concurrently` (Rails, PostgreSQL)
- `AddIndex` instead of `AddIndexConcurrently` (Django, PostgreSQL)
- `CREATE INDEX` without `CONCURRENTLY` (raw SQL, PostgreSQL)
- `add_index` / `ADD INDEX` without `ALGORITHM=INPLACE, LOCK=NONE` (MySQL)

**PostgreSQL**: a plain index build blocks writes. Use concurrent creation:
- Rails: `add_index :table, :column, algorithm: :concurrently` (requires `disable_ddl_transaction!`)
- Django: `AddIndexConcurrently` from `django.contrib.postgres.operations`, in a migration with `atomic = False`
- SQL: `CREATE INDEX CONCURRENTLY`, outside a transaction

**MySQL**: InnoDB builds secondary indexes online, but large builds lag replicas and wait on metadata locks. `CONCURRENTLY` does not exist and fails. State `ALGORITHM=INPLACE, LOCK=NONE` (Rails: `algorithm: :inplace`) so MySQL errors instead of locking, and use gh-ost or pt-online-schema-change for very large tables.

## 6. Running Data Migrations Inside Schema Migrations
- ActiveRecord queries inside Rails migration `change`/`up` methods
- ORM queries inside Django migration `RunPython`
- Eloquent queries inside Laravel migration `up()`

**Warning**: Mixing data and schema changes in one migration is risky. If the migration fails partway, the database may be left in an inconsistent state. Suggest:
- Separate schema changes and data backfills into distinct migrations
- Data migrations should be idempotent and reversible

## 7. Removing or Dropping Indexes
- `remove_index` (Rails)
- `RemoveIndex` (Django)
- `dropIndex` (Laravel)
- `DROP INDEX` (raw SQL)

**Warning**: Removing an index can cause severe query performance degradation. Verify:
- No queries depend on this index
- Check slow query logs or EXPLAIN plans
- Consider the index's role in foreign key lookups

## 8. Adding Unique Constraints to Populated Tables
- `add_index ..., unique: true` (Rails)
- `unique=True` on field (Django)
- `->unique()` (Laravel)
- `ADD CONSTRAINT ... UNIQUE` (raw SQL)

**Warning**: Will fail if duplicate values already exist. Suggest:
1. Query for duplicates first
2. Resolve duplicates before adding the constraint
3. Add the constraint in a separate migration after cleanup

## 9. Foreign Keys on Existing Tables
- `add_foreign_key`, `add_reference ..., foreign_key: true` (Rails)
- `AddField` with `ForeignKey` / `OneToOneField` (Django)
- `->foreign()`, `->constrained()` inside `Schema::table` (Laravel)
- `ALTER TABLE ... FOREIGN KEY` / `REFERENCES` (raw SQL)

**PostgreSQL**: takes a SHARE ROW EXCLUSIVE lock on both tables and checks every row. Add it `NOT VALID` (Rails: `validate: false`), then `VALIDATE CONSTRAINT` (Rails: `validate_foreign_key`) in a separate migration.
**MySQL**: copies the table and blocks writes unless `foreign_key_checks` is off. Check for orphaned rows first; for large tables use pt-online-schema-change (gh-ost does not support foreign keys).

## 10. CHECK Constraints and SET NOT NULL
- `add_check_constraint`, `change_column_null ..., false` (Rails)
- `AddConstraint` with `CheckConstraint` (Django)
- `ADD CONSTRAINT ... CHECK`, `ALTER COLUMN ... SET NOT NULL` (raw SQL)

**PostgreSQL**: both scan the table under ACCESS EXCLUSIVE. Add checks `NOT VALID` and validate separately. For NOT NULL on PostgreSQL 12+: add `CHECK (col IS NOT NULL) NOT VALID`, validate it, `SET NOT NULL` (now skips the scan), drop the check.
**MySQL**: no `NOT VALID`; both rebuild the table. Backfill or fix rows first, then use an online schema change tool for large tables.

## 11. MySQL Blocking Algorithms
- `ALGORITHM=COPY`, `LOCK=SHARED`, `LOCK=EXCLUSIVE`, Rails `algorithm: :copy`

**Warning**: blocks writes for the whole operation. Prefer `ALGORITHM=INSTANT` or `INPLACE` with `LOCK=NONE`, otherwise gh-ost or pt-online-schema-change. MySQL DDL is not transactional: keep one DDL change per migration.

## 12. Multi-Tenant Projects
Detect from dependencies: `ros-apartment` / `apartment` (Rails), `django-tenants` / `django-tenant-schemas` (Django), `stancl/tenancy` / `spatie/laravel-multitenancy` (Laravel), or a `database/migrations/tenant/` folder. These run each migration once per tenant schema or database:
- Lock time and runtime multiply by the tenant count
- A failure partway leaves tenants on different versions: make migrations idempotent
- Batch backfills per tenant and try one tenant first
- Commands: `rake apartment:migrate`, `python manage.py migrate_schemas`, `php artisan tenants:migrate`

Row-level tenancy (`acts_as_tenant`) shares one schema: every new unique index must include the tenant column, e.g. `add_index :users, [:account_id, :email], unique: true`.

## 13. Rehearsing on a Sentinel Database
For DANGEROUS changes, rehearse on a copy of production first. If `SENTINEL_DATABASE` is set (environment or `.env`), give the command that runs the migration against it, and time it:
- Rails: `DATABASE_URL="$SENTINEL_DATABASE" bin/rails db:migrate`
- Django: `DATABASE_URL="$SENTINEL_DATABASE" python manage.py migrate` (if settings read `DATABASE_URL`)
- Laravel: `DB_URL="$SENTINEL_DATABASE" php artisan migrate` (`DATABASE_URL` before Laravel 11)
- Prisma: `DATABASE_URL="$SENTINEL_DATABASE" npx prisma migrate deploy`
- Knex: `DATABASE_URL="$SENTINEL_DATABASE" npx knex migrate:latest` (if the knexfile reads it)

Never rehearse when `SENTINEL_DATABASE` equals `DATABASE_URL`, and never print its password. If it is not set, suggest restoring a recent backup and pointing `SENTINEL_DATABASE` at it.
