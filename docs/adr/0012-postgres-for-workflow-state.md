# ADR 0012: PostgreSQL for workflow state (access domain and checkpoints)

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Until M6 the access domain lived in memory, reseeded per process, and conversations were saved in a local SQLite file. An approval can wait for days and must survive restarts. Its records must also be shared by different processes: the employee's session, the manager's command, and the MCP servers. The spec lists `access_requests`, `employee_access` and `approvals` as PostgreSQL tables.

## Decision
- **`AccessStore` protocol** with two implementations: `InMemoryAccessStore` (tests, single-process demos) and `PgAccessStore`. A contract suite runs against both.
- **Migration 0003** adds `access_requests`, `approvals`, `employee_access` and id sequences. Seed rows are loaded by `aegisdesk db seed`/`init` with `ON CONFLICT DO NOTHING`, never at application startup.
- **Every state change is one conditional statement:** decide only if pending; change status only from an expected status; provision only if `provisioned_at IS NULL`, inside the same transaction as the grant; create with `ON CONFLICT (idempotency_key) DO NOTHING`.
- **LangGraph's `PostgresSaver`** (`CHECKPOINT_STORE=postgres`) keeps paused threads next to the domain data. Its tables are created by `aegisdesk db init`. SQLite remains the default for single-machine use.
- **Reference data stays in seed files** (employees, applications, assets, tickets) until the API milestone needs them in the database.

## Consequences
- The restart is proven with three separate processes in CI.
- Concurrency is safe without application locks: eight concurrent deciders produce exactly one winner (tested).
- Tests that use `AEGIS_TEST_DATABASE_URL` reset the three access-workflow tables, so that URL must point at a disposable database.
- Two persistence settings (`DATA_STORE`, `CHECKPOINT_STORE`). A mismatch, such as a durable store with an in-memory checkpointer, still works in one process but not across restarts. The docs recommend `postgres` for both.

## Alternatives considered
- **SQLite for everything:** no setup, but it doesn't suit several services, and it would move to PostgreSQL for the API anyway.
- **An ORM (SQLAlchemy models):** heavier than a few conditional statements. SQLAlchemy Core keeps the SQL, and its guarantees, visible.
