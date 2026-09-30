# ADR 0010: Append-only audit events in PostgreSQL

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Spec §35 requires an immutable-style audit event for every sensitive action. The event records:
- timestamp, request, trace and thread;
- the user;
- the agent and its version;
- the action, resource and tool;
- the policy decision;
- the approval and approver;
- the outcome.

Audit must be independent of conversation history, which lives in the LangGraph checkpointer and belongs to the user's thread.

## Decision
1. **Two events per tool call,** linked by `call_id`:
   - a *decision* event written before the tool runs;
   - an *outcome* event written after.

   If the decision event cannot be written for a write tool, the call is refused (`audit_unavailable`).
2. **An `audit_events` table** (Alembic migration 0002) with `BEFORE UPDATE OR DELETE` (row) and `BEFORE TRUNCATE` (statement) triggers that raise. The application API (`PgAuditLog`) has only `record` and `query`.
3. **IDs only.** `resource` holds identifiers taken from the validated arguments (ticket, application, document), never free text such as descriptions or justifications.
4. **`AUDIT_STORE=memory|postgres`.** The in-memory store is for tests and demos. With MCP over HTTP, Postgres is also the only way for the host and servers to share one trail.

## Consequences
- History cannot be edited through the application or by ordinary SQL. Changing that needs a reviewed migration.
- Writes cost one extra insert before and after each tool call (sub-millisecond locally).
- A superuser can still disable triggers. Stronger guarantees would need:
  - a separate audit database with its own credentials;
  - write-once storage or log shipping;
  - hash chaining.

  These are noted for the reliability milestone.
- Retention and erasure requests (e.g. GDPR) will need a documented process, since rows cannot be deleted in place.

## Alternatives considered
- **A hash-chained JSONL file:** tamper-evident and simple, but local to one process or machine, and not queryable.
- **Log lines only:** easy, but mixed with operational logs and without schema or retention.
- **Audit inside the conversation checkpoint:** rejected by the spec. Audit must be independent of conversational history.
