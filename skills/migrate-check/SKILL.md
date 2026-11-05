---
name: migrate-check
description: This skill should be used when the user creates or edits a database migration, mentions "migration safety", "schema change", "database migration", "add column", "drop column", "rename table", "migration review", or discusses database schema changes. Provides automatic safety analysis of migration files.
version: 1.1.0
---

# Migration Safety Check Skill

This skill automatically activates when database migration files are being created or modified, providing real-time safety guidance.

## When This Skill Applies

This skill activates when:
- The user creates a new migration file
- The user edits an existing migration
- The user asks about migration safety or best practices
- Schema change operations are being planned
- The user mentions dropping, renaming, or altering database columns/tables

## Framework Detection

- **Rails**: `db/migrate/`, files like `*_create_*.rb`
- **Django**: `migrations/` inside app directories, files like `0001_initial.py`
- **Laravel**: `database/migrations/`, PHP migration files
- **Prisma**: `prisma/migrations/`, `migration.sql` files
- **Knex/Sequelize**: `migrations/`, JS/TS files

## Database Engine and Tenancy

Before giving advice, find the engine: `config/database.yml` adapter, Laravel `.env` `DB_CONNECTION`, Prisma `provider`, or the driver in dependencies (`pg`/`psycopg` vs `mysql2`/`mysqlclient`/`trilogy`). PostgreSQL and MySQL need different fixes; if unknown, give both. Also check for multi-tenancy libraries and `SENTINEL_DATABASE` (patterns 12 and 13).

## Safety Rules

When a migration file is created or edited, check for these patterns. Per-framework syntax and the safe alternative for each are in `references/patterns.md`; read it when a pattern matches.

1. Dropping columns or tables (data loss, phased removal)
2. Renaming columns or tables (deploy downtime, add/backfill/swap/drop)
3. Changing column types (data compatibility, table locks)
4. NOT NULL without a default (fails on populated tables)
5. Index builds that block writes (PostgreSQL CONCURRENTLY, MySQL ALGORITHM=INPLACE)
6. Data migrations inside schema migrations (partial failure)
7. Removing indexes (query regressions)
8. Unique constraints on populated tables (duplicates)
9. Foreign keys on existing tables (validation scan and locks)
10. CHECK constraints and SET NOT NULL (full table scans)
11. MySQL ALGORITHM=COPY / LOCK=EXCLUSIVE (blocking rebuilds)
12. Multi-tenant fan-out and tenant-scoped unique indexes
13. Rehearsal on a sentinel database for DANGEROUS changes

## Risk Levels

- **SAFE**: adding nullable columns, adding indexes concurrently, creating new tables
- **WARNING**: adding NOT NULL without a default, blocking index builds, unique constraints, unvalidated foreign keys and CHECKs, SET NOT NULL, MySQL COPY/LOCK clauses, data changes inside schema migrations, changing defaults
- **DANGEROUS**: dropping columns/tables, renaming, type changes, removing indexes

## Safe Migration Checklist

- [ ] Migration is reversible (has a `down` method or rollback strategy)
- [ ] No data loss without explicit user confirmation
- [ ] Large table operations use concurrent/batched approaches
- [ ] Schema and data changes are separated
- [ ] NOT NULL columns have defaults or are added in phases
- [ ] Indexes are added concurrently for large tables
- [ ] Foreign key constraints reference valid tables/columns

## Framework-Specific Guidance

**Rails**:
- Recommend `strong_migrations` gem for automated checks
- Use `safety_assured` blocks only when risk is understood
- Ensure `disable_ddl_transaction!` is used with concurrent operations

**Django**:
- Use `AddIndexConcurrently` from `django.contrib.postgres.operations`
- Separate `RunPython` data migrations from schema operations
- Use `SeparateDatabaseAndState` for complex changes

**Laravel**:
- Use `doctrine/dbal` for column modifications
- Test migrations with `--pretend` flag first
- Use `Schema::hasColumn` guards for safety

**Prisma**:
- Check `prisma migrate diff` output before applying
- Use `@default` for new required fields
- Be cautious with `@map` and `@@map` renames

## Response Format

When this skill activates, prepend the migration analysis before any other response:

```
**Migration Safety Analysis**
Risk Level: SAFE / WARNING / DANGEROUS
Engine: PostgreSQL / MySQL / unknown, Tenancy: <library or none>

[List of issues found, if any]
[Suggested fixes, if any]
```
