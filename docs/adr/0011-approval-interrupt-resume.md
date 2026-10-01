# ADR 0011: Approval as a graph interrupt, with the store as the source of truth

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Spec §5 steps 10–14 require: create an approval request, pause, receive a manager's decision, resume, then provision. The LLM must not be able to bypass these steps. The pause must survive a restart, and approval operations must be auditable and idempotent. The user chose to resume the workflow as soon as the decision is recorded.

## Decision
1. **Approval steps are created with the access request.** The same store operation creates them, and approvers are resolved by code: the requester's manager, or a role.
2. **The graph pauses with `interrupt()`** in a deterministic `await_approval` node after `respond`. The checkpointer persists the thread. Resuming re-runs the node, which **re-reads the approval store**. The resume value is ignored; if steps remain, the node pauses again.
3. **Decisions go through `ApprovalService`.** It checks visibility, the named approver or role, separation of duties and expiry, then makes a conditional update, finalises the request and writes an audit event. The CLI then resumes the thread immediately (`ThreadedGraphAgent.resume`, no model call).
4. **Provisioning is a HIGH-risk tool** (`provision_access`) granted only to the `access_workflow` identity. The gateway allows it only with **approval evidence it looks up itself**, and records `approval_id` and `approver_id` in the audit event.
5. **Standard applications** (`auto_approved`) are provisioned in the same turn through the same governed tool, with automatic evidence.

## Consequences
- A manipulated model cannot grant access. It has no provisioning tool, and even an LLM agent that holds it is denied by policy. Approval text in its answer changes nothing. It cannot create or alter approval records.
- A forged or early resume is harmless, because the store decides.
- The workflow is restart-safe, provided both the checkpoint and the store are durable.
- The user sees an extra message in the same thread when the decision lands, not a new conversation.
- Cost: the decider's process runs the resume, including provisioning through MCP if configured. A production API would put this on a worker queue.

## Alternatives considered
- **Let the Access agent call `provision_access` after approval:** it puts a model back in the grant path. Rejected.
- **Interrupt inside the specialist subgraph:** specialists run without a checkpointer (context isolation, ADR 0007), and pausing at parent level keeps them simple.
- **Resume only when the employee returns:** simpler, but the result would wait on the employee. The user chose immediate resume.
- **Trust the resume payload** (`{"approved": true}`): anyone able to resume could approve. Rejected.
