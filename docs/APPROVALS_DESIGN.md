# Approvals design

Walkthrough, transcript and test results: [M7 notes](milestones/M7-approvals.md). Decisions:
- [ADR 0011](adr/0011-approval-interrupt-resume.md): interrupt and resume.
- [ADR 0012](adr/0012-postgres-for-workflow-state.md): Postgres for workflow state.

## Approval workflow

```
 Employee                     Host (graph)                          Stores                  Approver
 ────────                     ────────────                          ──────                  ────────
 "I need FinanceERP" ─► router → access agent
                               create_access_request ──(gateway: MEDIUM, allowed)──► access_requests AR-1013
                                                                                 └► approvals AP-0001 (manager=E1010)
                               respond: "recorded, waiting for AP-0001"
                               await_approval: store says pending
                               INTERRUPT ─────────────────────────────────► checkpoint (thread T)
 ◄── answer + "⏸ waiting for AP-0001"             (process may exit / restart)

                                                                                  approvals list --as E1010
                                                                                  approvals approve AP-0001
                               ApprovalService.decide ◄─────────────────────────── (who? SoD? expired? pending?)
                                 conditional update ───────────────► approvals AP-0001 = approved
                                 settle request   ───────────────► access_requests AR-1013 = approved
                                 audit approval_decision ──────────► audit_events
                               resume(T): Command(resume=…)
                               await_approval: store says all decided
                               apply_approvals:
                                 provision_access as access_workflow
                                   gateway: HIGH + evidence(store) → allow, audit approval_id/approver_id
                                   handler: once ─────────────────► employee_access + provisioned_at
                                 "Update on AR-1013: … granted" ──► checkpoint (thread T)
 thread T --as E1004 ◄── the update is in the employee's conversation
```

## State machine

```
access request:  awaiting_approval ──(all steps approved)──► approved ──provision──► (provisioned_at set)
                        │                                                            (exactly once)
                        └──(any step rejected or expired)──► rejected
                 auto_approved ──provision (same turn, no pause)──► (provisioned_at set)

approval step:   pending ──► approved | rejected | expired     (only from pending; conditional update)
```

## Rules

| Rule | Where |
|---|---|
| which steps a request needs | `evaluate_eligibility` (application approvals, contractor sponsor) |
| who approves each step | `approval_steps()`: manager_id, else role |
| who may see / decide | `ApprovalService.why_not` / `_may_view` |
| separation of duties | requester excluded; one person per request |
| expiry | `APPROVAL_TTL_HOURS`; `refresh()` expires and closes |
| idempotency | conditional updates; same decision → no change; one-shot provisioning |
| provisioning allowed | policy: HIGH + gateway-found evidence; agent `access_workflow` only |
| resume source of truth | the store; the resume value is ignored |

## Storage

| Table (migration 0003) | Notes |
|---|---|
| `access_requests` | `idempotency_key` unique; `thread_id` (to resume); `provisioned_at` (once) |
| `approvals` | unique (request, step); `approver_id` or `approver_role`; `decided_by/at`, `comment`; `expires_at` |
| `employee_access` | `access_request_id` unique (a request grants at most one row) |
| LangGraph checkpoint tables | created by `PostgresSaver.setup()` in `aegisdesk db init` |

Configuration: `DATA_STORE=memory|postgres`, `CHECKPOINT_STORE=sqlite|postgres`, `APPROVAL_TTL_HOURS` (default 168), `DATABASE_URL`.
