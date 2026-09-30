# Milestone 7: Human approval (interrupt, persist, resume)

**Goal:** a sensitive access request pauses the workflow and waits for the right people to approve it. The pause survives restarts. A decision resumes the *original* conversation, and access is granted only through the governed, audited path. The model cannot skip or fake any of it.

**What you can run now** (PostgreSQL; `docker compose up -d postgres` or any local instance):

```bash
export DATABASE_URL=postgresql+psycopg://aegisdesk:aegisdesk@localhost:5432/aegisdesk
export DATA_STORE=postgres CHECKPOINT_STORE=postgres AUDIT_STORE=postgres
uv run aegisdesk db init          # migrations + LangGraph checkpoint tables + seed rows

# 1. the employee asks (process exits while the request waits)
uv run aegisdesk agent --as E1004 "Please create an access request for FinanceERP for month-end reporting"
# 2. the manager, in another process (or after a restart)
uv run aegisdesk approvals list --as E1010
uv run aegisdesk approvals approve AP-0001 --as E1010 --comment "Month-end close"
# 3. the employee's conversation now ends with the outcome
uv run aegisdesk thread <thread-id> --as E1004
uv run aegisdesk audit --user E1004
```

Without Postgres, everything still works inside one process (tests, demos). `approvals list` explains that an in-memory store cannot be shared between processes.

Design reference: [`docs/APPROVALS_DESIGN.md`](../APPROVALS_DESIGN.md). Decisions:
- [ADR 0011](../adr/0011-approval-interrupt-resume.md): interrupt and resume.
- [ADR 0012](../adr/0012-postgres-for-workflow-state.md): Postgres for workflow state.

---

## 1. Concepts introduced

### Human-in-the-loop as a graph pause, not a prompt
The spec's steps 10–14 (create approval, pause, receive decision, resume, provision) are **graph structure and code**, not model behaviour:

```
… access agent ─► respond ─► await_approval ──► INTERRUPT  (state checkpointed; process may exit)
                                  ▲    │
                  still waiting ──┘    │ every step decided (read from the store)
                                       ▼
                               apply_approvals ─► provision_access (HIGH, via the gateway)
                                               └► "Update on AR-1013: … Access has been granted."
```

`await_approval` calls LangGraph's `interrupt()`. The checkpointer has already saved the thread, so the process can exit. Resuming (`Command(resume=…)`) re-runs the node. The node re-reads the **approval store**, so the resume value is informational only; a forged one changes nothing (tested). If only some steps are decided (manager yes, security pending), the node pauses again.

### Durable state: two things must survive
1. **The paused conversation:** the LangGraph checkpoint (`CHECKPOINT_STORE=sqlite|postgres`).
2. **The approval records:** requests, approval steps, granted access (`DATA_STORE=postgres`, migration 0003).

If either were in memory, a restart would lose the request. Proven with three separate processes in `test_approval_resumes_the_workflow_in_another_process`.

### Approvals are created with the request
When `create_access_request` records a request that needs approval, the same store operation creates one approval step per required approval. Each step names who decides:

| Step | Approver |
|---|---|
| manager | the requester's `manager_id` (a named person); any other `manager` if the requester has none |
| security | anyone with role `security_approver` (E1015 Ines Duarte) |
| data_owner | anyone with role `data_owner` (E1016 Viktor Lindqvist) |

The agent never chooses the approver. The model only sees the result, e.g. `"Waiting for approval: AP-0001 (manager: E1010)"`.

### Who may decide (deterministic, `approvals/service.py`)
1. You can only see steps you may decide, or your own requests. Anything else is `not_found`, so approval IDs cannot be probed.
2. Named approver, or holder of the step's role.
3. **Separation of duties:** never the requester, and never the same person for two steps of one request.
4. Not expired (`APPROVAL_TTL_HOURS`, default 7 days). An overdue step expires and closes the request.
5. **Idempotent:** the same decision again returns the recorded one; a different decision on a decided step is refused. The store update is conditional (`WHERE status = 'pending'`); eight concurrent deciders → exactly one wins (tested).

Every decision, including refusals, writes an audit event (`action=approval_decision`, `approval_id`, `approver_id`).

### Provisioning is HIGH risk and needs evidence the gateway finds itself
The only tool that grants access is `provision_access`:
- HIGH risk;
- on the MCP action server;
- granted in `policy.yaml` to exactly one identity, `access_workflow`, which is code, not a model.

The M6 rule "HIGH needs human approval" now has its other half. The gateway asks `AccessApprovalVerifier` for **approval evidence**, looked up in the store and never taken from the caller:
- the request belongs to the calling user;
- it is `approved` (every step) or `auto_approved` (standard app);
- it is not yet provisioned.

With evidence, the policy allows it, and the audit event carries `approval_id` and `approver_id`. This is the first time those spec fields are filled. Without evidence, the call is `approval_required` and the handler never runs.

Standard applications (`auto_approved`) are provisioned immediately, in the same turn, with no pause. Privileged applications are granted for 30 days.

---

## 2. What was built

| File | Purpose |
|---|---|
| `src/aegisdesk/domain/access.py` | `ApprovalRecord`, `ApprovalStep`, `approval_steps()` (who approves), request `thread_id` / `provisioned_at` |
| `src/aegisdesk/domain/access_store.py` | `AccessStore` protocol + `InMemoryAccessStore` |
| `src/aegisdesk/domain/access_store_pg.py` | `PgAccessStore`: conditional updates, idempotent create, one-shot provisioning, seeding |
| `migrations/versions/0003_access_workflow.py` | `access_requests`, `approvals`, `employee_access`, id sequences |
| `src/aegisdesk/approvals/service.py` | `ApprovalService`: authorization, separation of duties, expiry, idempotency, audit |
| `src/aegisdesk/approvals/evidence.py` | `AccessApprovalVerifier`: approval evidence for the gateway |
| `src/aegisdesk/approvals/workflow.py` | `AccessApprovalWorkflow`: requests of a turn, waiting?, pending steps, settle (provision/report) |
| `src/aegisdesk/tools/provisioning.py` | `provision_access` (HIGH, workflow only, grants at most once) |
| `src/aegisdesk/graphs/supervisor_graph.py` | `await_approval` (interrupt) and `apply_approvals` nodes |
| `src/aegisdesk/graphs/service_desk_graph.py` | `ThreadedGraphAgent.resume()`, `pending_approvals`; answers include every message of the turn |
| `src/aegisdesk/governance/*` | `ApprovalEvidence` in `PolicyInput`; gateway `ApprovalVerifier`; audit `approval_id` / `approver_id` |
| `src/aegisdesk/persistence/factory.py` | `build_repository` (DATA_STORE), `open_checkpointer` (CHECKPOINT_STORE) |
| `src/aegisdesk/cli.py` | `approvals list/show/approve/reject`, `db init/seed`, the pause shown by `agent` |
| `config/policy.yaml` | `provision_access: high`, `access_workflow: [provision_access]` |
| `data/seed/employees.json` | E1015 (security approver), E1016 (data owner) |

Also fixed: audit `resource` could contain model-written text when a model put a sentence in `application` (found in this milestone's demo). Only identifier-shaped values are kept now (`test_audit_resource_keeps_identifiers_only`).

---

## 3. Demo transcript: two-step approval across processes

Real output (fake model; `DATA_STORE=postgres CHECKPOINT_STORE=postgres AUDIT_STORE=postgres`). Every command is a separate process:

```
$ aegisdesk agent --as E1006 --quiet "Please create an access request for ProductionDB to follow up the P1 incident"
Assistant: [fake model] Tool results: {"request_id":"AR-1014","application":"ProductionDB","status":"awaiting_approval",
  "approvals_required":["manager","security"],"created":true,"next_step":"Waiting for approval: AP-0002 (manager: E1013);
  AP-0003 (security: any security approver). Nothing is granted until every approval is recorded."}
⏸ Waiting for approval: AP-0002 (manager: E1013), AP-0003 (security: any security_approver). Thread 1ce4856e-… will resume on decision.

$ aegisdesk approvals approve AP-0002 --as E1013 --comment "Needed for INC follow-up"
AP-0002  approved manager    AR-1014 ProductionDB for Lena Hoffmann (E1006)  approver=E1013  … by E1013 "Needed for INC follow-up"
  AR-1014 still waits for other approvals.

$ aegisdesk approvals approve AP-0003 --as E1015 --comment "30 days, read-only"
AP-0003  approved security   AR-1014 ProductionDB for Lena Hoffmann (E1006)  approver=any security_approver  … by E1015 "30 days, read-only"
Resumed thread 1ce4856e-…:
Assistant: Update on AR-1014 (ProductionDB): approved by manager: Kenji Watanabe (E1013) (AP-0002), security: Ines Duarte (E1015)
  (AP-0003). Access has been granted. It expires on 2026-10-30.

$ aegisdesk audit --user E1006
… decision create_access_request  agent=access@0.1.0          decision=allow outcome=allow
… outcome  create_access_request  agent=access@0.1.0          decision=allow outcome=ok
… outcome  decide_approval        agent=-                     decision=human outcome=approved approval=AP-0002 approver=E1013
… outcome  decide_approval        agent=-                     decision=human outcome=approved approval=AP-0003 approver=E1015
… decision provision_access       agent=access_workflow@0.1.0 decision=allow outcome=allow approval=AP-0002,AP-0003 approver=E1013,E1015 thread_id=1ce4856e-…
… outcome  provision_access       agent=access_workflow@0.1.0 decision=allow outcome=ok    approval=AP-0002,AP-0003 approver=E1013,E1015
```

Refusals seen in the same session (FinanceERP for E1004):
- **Another manager (E1011):** `Refused (not_found)`.
- **The requester (E1004):** `Refused (cannot_approve_own_request)`.
- **Repeat approval by E1010:** `(already recorded; nothing changed)`, then `Thread … is not paused (already resumed)`.

---

## 4. Tests (real results)

Full suite with PostgreSQL integration enabled (`AEGIS_TEST_DATABASE_URL`, migrations 0001–0003):

```
390 passed, 22 skipped in 35.75s
```

The skips are the 21 `live` provider tests (no `ANTHROPIC_API_KEY`, no Ollama here) and one contract case that only applies to PostgreSQL (`seed`). `ruff check`, `ruff format --check` and `mypy` (strict, 128 files) are clean. The retrieval gate still passes (hit rate 0.89, 0 access violations).

New in this milestone:

| File | Tests | What it proves |
|---|---|---|
| `tests/unit/test_approvals.py` | 13 | pause → approve → resume → granted (+ audit with approval and approver ids); reject; ProductionDB two steps by two people; separation of duties; standard app granted without pause; repeated decisions/resumes grant once; premature resume pauses again; **restart** over a SQLite checkpoint file; only the right manager sees/decides; pending list per approver; expiry closes the request; refusals audited; resume **over MCP** with server-side enforcement |
| `tests/security/test_approval_bypass.py` | 6 | the model calls `provision_access` (`unknown_tool`) and claims approval in text (still paused); an LLM agent holding the tool is denied by policy; the workflow without approval → `approval_required`; someone else's approval is not evidence; provisioning happens exactly once; a forged resume value is ignored |
| `tests/integration/test_access_workflow_postgres.py` | 13 | the store contract against memory **and** PostgreSQL (create, idempotency, conditional decisions and status, one-shot provisioning, 8 concurrent deciders → 1 winner, idempotent seed); **three separate CLI processes**: pause, approve and resume, read the thread, audit |
| `tests/unit/test_cli.py` | +3 | pause shown by `agent`; memory-store hint; unknown approval refused |
| `tests/unit/test_gateway.py` | +1 | audit resources keep identifiers only |
| updated | | topology (+2 nodes), action server now has one HIGH tool, only `access_workflow` may provision |

---

## 5. Not built yet

- **Approval UI and API** (M10): `GET/POST /api/v1/approvals…` and the Streamlit manager view. The CLI plays that role now.
- **Notifications and reminders:** approvers learn about work via `approvals list`; no email or chat.
- **Escalation on timeout:** an expired step closes the request; it does not escalate to the next manager.
- **Approval for other HIGH actions:** the evidence verifier covers `provision_access`. New HIGH tools need their own evidence rule, and until they have one they are always `approval_required`.
- **Separate processes for MCP servers:** with `mcp_http`, the servers now share the approval store through PostgreSQL. A full multi-service deployment is M10/M12.
