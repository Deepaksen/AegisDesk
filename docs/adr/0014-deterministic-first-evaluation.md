# ADR 0014: Deterministic-first evaluation, split gates, scripted adversarial trajectories, opt-in judge

* **Status:** Accepted
* **Date:** 2026-09-30

## Context
Spec §28–§31 ask for a 60-case golden dataset; deterministic, RAG and trajectory evaluators; LLM judges; adversarial and performance evaluation; a comparison of two system versions; and quality gates, several of which are safety properties (0 unauthorized actions, 100% approval for high-risk actions, 100% traced, no secrets in telemetry).

CI and this environment have no model credentials. The offline fake model is deterministic but not intelligent (keyword routing, one task per request), so its quality numbers say little about a real model. The user chose to compare architectures (multi-agent vs single agent) on the offline model, with the same runner comparing models when credentials exist, and an opt-in real judge.

## Decision
1. **Deterministic first.** Everything a program can verify is checked in code from what the run *did*: trajectory, audit events, spans, logs, and the data written (before/after snapshots of a fresh seeded store per case). No model grades routing, tool choice, policy, approvals, citations or effects.
2. **Three gates with different jobs.**
   - *Safety* gates hold for any model and run in CI on the offline model.
   - *Quality* gates are the spec's targets; they measure a model and are enforced with `--quality-gate`, meaningfully with a real model.
   - *Regression* gates compare against committed baselines per dataset, config and model.
3. **Attacks are scripted trajectories.** Security and adversarial cases replace the model with a `ScriptedChatModel` that emits the manipulated routes and tool calls. The criterion is "no unauthorized action whatever the model outputs", so the result must not depend on how easily a particular model is fooled. Model susceptibility is a separate, quality-side question.
4. **Unauthorized is not the same as unwanted.** A write the user was allowed to make but did not ask for fails the task (`effects`), not the safety gate. Unauthorized means: a grant to someone else, a grant without approval, or, in an attack case, any write beyond what the case allows.
5. **The judge is opt-in and never gates.** Versioned prompt, structured verdict, configured judge model on the allowlist; otherwise "not run". No fallback to the fake model.
6. **Costs come from a dated price table** (`config/pricing.yaml`); unknown models report no cost rather than a guess.

## Consequences
- CI proves safety properties on every change in about 25 seconds, without credentials.
- The adversarial suite found a real gap (a manipulated model could create six tickets in one request). It is fixed deterministically: a per-request write budget in the policy (`limits.max_writes_per_request`), counted by the gateway from the audit trail.
- Quality numbers in the repository describe the offline model and the architectures, not a real model; real-model numbers must be produced by running the same command with credentials.
- Baselines must be regenerated deliberately (`--write-baseline`) after intended behaviour changes; a missing baseline fails the gate.
