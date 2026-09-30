# ADR 0009: A deterministic policy engine in Python, with policy as data

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Spec §7 and §14 require tool calls to be authorized by deterministic policy middleware, never by a "governance agent" or by prompts. The policy has to cover:
- agent-to-tool authorization;
- user authorization;
- risk classes (LOW automatic, MEDIUM only when explicitly authorized, HIGH needs human approval);
- forbidden actions;
- environment restrictions.

The spec names OPA/Rego "or an equivalent deterministic policy mechanism". The policy must be enforced on both sides of the MCP boundary.

## Decision
1. **Policy as data.** `config/policy.yaml` holds risk classes, grants, authorized writes, forbidden tools, environment rules and role requirements. It is validated strictly at startup (unknown keys, unclassified tool names, and tools both classified and forbidden are all errors). An invalid file stops the application.
2. **A small Python engine** (`governance/policy.py`) evaluates it:
   - It collects every deny reason (like a Rego `deny` set). Any deny → DENY; else HIGH → REQUIRE_APPROVAL; else ALLOW.
   - Unknown tools, unknown agents and engine errors are denies.
3. **An OPA-shaped contract.** The input is `{tool, user, agent, environment}` and the output is `{decision, reasons, policy_version}`. An OPA client could replace the engine behind the same interface.
4. **One gateway, two enforcement points.** `ActionGateway` runs inside `ToolExecutor`, between validation and execution, in the host (local tools) and in the MCP servers (from the token's agent).
5. **The policy's risk class is authoritative** over the tool's own metadata. Tests assert that the two agree, and that the host allowlists equal the policy grants.

## Consequences
- The policy can be reviewed as data, and each decision is recorded with the version of the policy file that made it.
- No extra service to run; in-process evaluation takes microseconds; tests need no OPA binary.
- We lose Rego's expressiveness, its policy tooling (`opa test`, bundles, decision logs) and a language-neutral policy that other services could share. If policies grow beyond grants and classes (attribute-based rules over resources, time windows), OPA becomes the better tool, and the contract above keeps that migration small.
- Record-level checks (e.g. ticket ownership) stay in the tools, which can see the record.

## Alternatives considered
- **OPA server with Rego:** the standard choice and closest to the spec's wording. It needs a sidecar or binary in every environment, plus fail-closed handling when it is unreachable. Deferred by choice for this milestone.
- **Rego compiled to Wasm and evaluated in-process:** real Rego without a server, but it needs a Wasm runtime and custom ABI glue.
- **Rules as Python `if` statements spread through tools:** fast to write, but hard to review, and there would be no single decision to audit.
- **An LLM "governance agent":** rejected by the spec. Its decisions can be manipulated.
