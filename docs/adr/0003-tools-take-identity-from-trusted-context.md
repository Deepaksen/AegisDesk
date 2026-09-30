# ADR 0003: Tools take identity from trusted context, never from model arguments

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Service Desk tools act on behalf of an employee: "my assets", "my tickets", "create a ticket for me". The obvious design is `get_assets(employee_id)`, with the model filling in the ID from the conversation. But the model's arguments are derived from untrusted text: the user's message, earlier turns, and, from M3, retrieved documents. A user who types "I'm E1002" or a document that says "look up the CFO's laptop" could steer that argument. The spec (§13, §40) forbids trusting employee IDs supplied through conversation.

## Decision
1. **No identity parameters in model-facing schemas.** `get_my_assets` takes no arguments. `get_ticket` takes only a ticket ID, and `create_ticket` has no requester field.
2. **Identity travels outside the model.** The application authenticates the user before the agent runs and passes a `UserContext` to `ToolExecutor.execute`, which hands it to handlers through `ToolCallContext`.
3. **Strict schemas.** All tool input models use `extra="forbid"`, so a model that adds `employee_id` or `requester_id` gets `invalid_arguments` and nothing runs.
4. **Ownership checks are deterministic, and "not yours" equals "not found".** A request for another employee's ticket returns the same `not_found` as a nonexistent ticket, so existence cannot be probed.
5. **Writes get an application-derived idempotency key** (user, request ID, tool, validated arguments). The model does not supply it.

## Consequences
* A fully compromised model can, at worst, read and write the signed-in user's own data through the tools it was given. That is the same as the user acting directly.
* Security tests can script a malicious model and assert that nothing unauthorized happens, without any real LLM.
* Tools that genuinely need to act on another person (a manager approving a report's request, an IT admin looking up any asset) cannot simply take a free-form ID. They will take a *resource* argument, and a policy decision (OPA, M6) will check the relationship between the trusted user and that resource.
* Ownership logic currently lives in each handler. M6 centralises it in a Tool Gateway with a policy engine, so tools cannot forget it.

## Alternatives considered
* **Accept `employee_id` and check it equals the session user:** safe if every tool remembers the check, but it offers the model a parameter that can only ever have one valid value, which invites confusion and makes a missing check a data leak.
* **Tell the model in the system prompt not to look up other people:** useful for good behaviour, but not a control. Prompts can be overridden by injection. We keep the instruction in `service_desk@v1` for tone and user experience, and enforce the rule in code.
