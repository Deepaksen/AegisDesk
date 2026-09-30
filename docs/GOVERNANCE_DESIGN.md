# Governance design

Walkthrough and test results: [M6 notes](milestones/M6-governance.md).

Decisions:
- [ADR 0009](adr/0009-deterministic-policy-engine.md): the policy engine.
- [ADR 0010](adr/0010-append-only-audit-store.md): the audit store.

## Security boundary (after M6)

```
 model output (untrusted)
      │ tool name + JSON args
      ▼
 HOST ─────────────────────────────────────────────────────────────────────────
 1. per-agent allowlist (agents/supervisor.py)          → unknown_tool
 2. local tools: ToolExecutor
      schema validation (strict, no identity fields)    → invalid_arguments
      ACTION GATEWAY: policy(agent, user, tool, env)    → policy_denied / approval_required
                      audit decision (fail closed for writes)
      handler: ownership checks, idempotency key        → not_found / not_eligible …
      audit outcome
 3. remote tools: RemoteToolRunner
      signed delegation token (user, act=agent, rid, tid, aud=server, 60 s)
      timeout, read-only retry, no write retry
 ────────────────────────────────────── MCP ───────────────────────────────────
 SERVER (aegisdesk-read / aegisdesk-action)
 4. verify token (sig, iss, aud, exp, claims)            → unauthenticated
 5. ToolExecutor with ACTION GATEWAY, agent = token's act → same checks as 2
 ──────────────────────────────────────────────────────────────────────────────
 audit_events (PostgreSQL): INSERT/SELECT only; UPDATE/DELETE/TRUNCATE rejected by triggers
```

Checks 2 and 5 run the same code and the same policy file. For enterprise tools over MCP, check 5 is the one that counts: it trusts nothing from the host except the signature.

## Policy input and decision

```
PolicyInput    {tool, user: {employee_id, roles, department, manager_id},
                agent: {agent_id, agent_version, agent_type, environment} | null,
                environment}                      # where the check runs
PolicyDecision {decision: allow | deny | require_approval,
                reasons: [...], policy_version: "v1-<sha256[:12]>", risk}
```

## Current policy (`config/policy.yaml`)

| Agent | Tools |
|---|---|
| knowledge | search_knowledge_base, retrieve_document, request_handoff |
| service_desk | get_my_assets, list_my_tickets, get_ticket, create_ticket, add_ticket_comment, search_knowledge_base, request_handoff |
| access | get_employee_profile, list_my_access, get_application, check_access_eligibility, create_access_request, request_handoff |
| service_desk_single | the Service Desk tools + both knowledge tools (graph/loop engines) |
| supervisor | none (not listed → unknown_agent) |

| Risk | Tools | Rule |
|---|---|---|
| LOW | all reads, knowledge, handoff | allowed if granted |
| MEDIUM | create_ticket, add_ticket_comment, create_access_request | allowed only if in `authorized_writes` (all three are) |
| HIGH | provision_access (granted only to `access_workflow`) | require approval, unless the gateway finds recorded approval evidence in the store (M7) |
| forbidden | direct_grant_production_admin, grant_access, delete_audit_events | always denied |

| Environment | Allowed |
|---|---|
| development, test | all classified tools (writes go to the simulated systems) |
| production | reads and knowledge only, until write integrations are approved |

## Audit event

| Field | Notes |
|---|---|
| event_id, occurred_at | uuid, UTC |
| phase, call_id | decision / outcome; links the two events of a call |
| request_id, trace_id, thread_id | trace_id from M8; thread_id travels in the token's `tid` claim over MCP |
| user_id, agent_id, agent_version, environment | from the session or verified token |
| action, tool, risk, resource | resource = IDs from validated args only |
| policy_decision, policy_reasons, policy_version | the version pins which policy file decided |
| approval_id, approver_id | M7 |
| outcome, latency_ms | decision value, or ok / error category |

## Failure handling

| Failure | Behaviour |
|---|---|
| policy file missing/invalid | startup refused (`Configuration error`, exit 2) |
| exception inside policy evaluation | deny, `policy_error` |
| no agent identity on a governed call | deny, `unknown_agent` |
| audit write fails before a write/unknown-risk tool | deny, `audit_unavailable` |
| audit write fails before a read | read runs; error logged |
| audit write fails after the tool | error logged (the action already happened; its decision event exists) |
