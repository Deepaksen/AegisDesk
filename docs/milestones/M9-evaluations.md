# Milestone 9: Evaluations

**Goal:** measure the system instead of trusting demos. A 60-case golden dataset and an adversarial suite run through the real system. Code checks what happened (trajectory, policy, approvals, citations, writes, traces, secrets), an optional LLM judge grades what code cannot, and gates stop regressions and safety failures in CI. Two versions of the system are compared on the same cases.

**What you can run now:**

```bash
uv run aegisdesk eval golden                                        # 60 cases, multi-agent, offline model
uv run aegisdesk eval golden --config multi --compare single        # multi-agent vs single agent
uv run aegisdesk eval golden --dataset evals/adversarial/security_v1.yaml --config multi_mcp
uv run aegisdesk eval golden --baseline evals/regression/golden_v1-multi-fake-scripted.json
```

Design: [`docs/EVALUATION.md`](../EVALUATION.md). Decision: [ADR 0014](../adr/0014-deterministic-first-evaluation.md).

---

## 1. Concepts introduced

### Golden dataset
A fixed, versioned set of requests with what *should* happen: which agents run, which tools are (and are not) used, what the policy decides, whether approval is needed, which facts and citations the answer contains, and exactly what may be written. Versioned (`golden_v1.yaml`) so numbers from different days are comparable. The 60 cases follow the spec's distribution: 15 knowledge, 10 service desk, 15 access, 8 multi-intent, 8 security, 4 failure.

### Check what happened, not what was said
The runner captures the whole run: trajectory, audit events (M6), spans (M8), JSON logs, and a before/after snapshot of the data store. So "did it open a ticket" is answered by the data, "was approval enforced" by the access store and the workflow pause, "is every tool traced" by the spans. An answer claiming "access granted" proves nothing.

### Deterministic, RAG, trajectory, judge
- **Deterministic:** exact properties (policy decision, effects, approval, limits, secrets).
- **RAG:** citations present, citations grounded (cited ⊆ retrieved), retrieval recall.
- **Trajectory:** routing, required/forbidden tools, tool arguments, tool-call and model-call budgets.
- **LLM judge:** only qualities code cannot check (completeness, clarity, semantic correctness, groundedness, helpfulness), with a versioned prompt and a structured verdict. Opt-in, never a gate.

### Adversarial evaluation with scripted trajectories
To test "no unauthorized action whatever the model outputs", the model is replaced by a script of the manipulated outputs: injected handoffs, identity fields in arguments, path traversal, oversized arguments, write bursts, secrets in arguments. The system under test is everything *except* the model: tool isolation, policy, approvals, schemas, telemetry redaction.

### Three kinds of gate
Safety (must hold for any model; CI), quality (the spec's targets; measure a model), regression (no drop against a committed baseline for the same dataset, config and model).

### Unauthorized vs unwanted
A ticket the user could open but did not ask for is a *task failure* (`unexpected_writes`). An *unauthorized* action is one the user was not allowed to cause: a grant to someone else, a grant without approval, or any write beyond what an attack case allows. Mixing the two would make the safety gate either useless or permanently red.

## 2. What was built

| Piece | Where |
|---|---|
| Dataset schema, loader | `src/aegisdesk/evals/golden.py` |
| Golden dataset (60) | `evals/datasets/golden_v1.yaml` |
| Adversarial suite (8) | `evals/adversarial/security_v1.yaml` |
| Runner: fresh world per case, scripted models, repeats, approvals + resume, faults, captured spans/logs/effects | `src/aegisdesk/evals/runner.py` |
| Checks and measurements | `src/aegisdesk/evals/evaluators.py` |
| Metrics, gates, pricing, comparison | `src/aegisdesk/evals/report.py`, `config/pricing.yaml` |
| LLM judge | `src/aegisdesk/evals/judge.py`, `prompts/judge/v1.yaml` |
| CLI `eval golden` | `src/aegisdesk/cli.py` |
| Baselines | `evals/regression/*.json` |
| CI gates | `.github/workflows/ci.yml` |

System versions compared: `multi` (supervisor + specialists, M4–M8), `multi_mcp` (the same, tools over MCP), `single` (the single Service Desk agent, M2/M3). The single agent has no supervisor, so scripted multi-agent cases (routes) and MCP fault cases are skipped for it.

## 3. What the evaluation found (and fixed)

Running the suites surfaced real problems, all fixed in this milestone:

1. **A write burst was allowed.** `adv-05` scripts a manipulated model that creates six tickets in one request. Every call was individually authorized, so six tickets were created: `unauthorized_actions 1`, safety gate red. Fix: a per-request write budget in the policy (`limits.max_writes_per_request: 3` in `config/policy.yaml`), evaluated by the policy engine and counted by the gateway from the audit trail (allowed MEDIUM/HIGH decisions with the same request ID; if the audit cannot be read, it fails closed). New deny reason `write_budget_exceeded`. After the fix: three tickets, calls four to six denied and audited.
2. **Refused unknown tools over MCP had no span.** `trace_coverage` was 0.97 on `multi_mcp`: when the remote runner refused a tool outside the agent's allowlist, nothing was traced. Fix: `refused_unknown_tool()` in `tools/executor.py` emits an `execute_tool` span (error `unknown_tool`) and metrics, used by both the local and remote runners.
3. **Metric design errors** caught by reading results rather than trusting them: unauthorized vs unwanted writes (above); "approval coverage" first measured routing, now it measures enforcement; latency first included building the system, now only request time; one expectation (`sec-05`) was wrong, since one ticket is the correct outcome for an oversized request.

## 4. Results (offline fake model, real runs)

All numbers below come from actual runs in this environment on the offline `fake/fake-scripted` model. **They describe the architectures and the offline model, not a real LLM.** The fake routes by keywords and produces a single routing task per request, so multi-intent and many knowledge cases fail by design. No real-model numbers are reported because no credentials were available.

### Multi-agent vs single agent (golden v1)

| metric | multi | single |
|---|---|---|
| cases run (skipped) | 60 (0) | 52 (8) |
| task success | 0.617 | 0.327 |
| routing accuracy | 0.652 | n/a |
| tool selection accuracy | 0.512 | 0.415 |
| policy accuracy | 1.000 | 0.333 |
| citation rate | 0.250 | 0.375 |
| approval expectation accuracy | 0.857 | 0.231 |
| approval coverage (enforced) | 1.000 | n/a |
| trace coverage | 1.000 | 1.000 |
| unauthorized actions | 0 | 0 |
| unexpected writes | 3 | 16 |
| secret leaks | 0 | 0 |
| security pass rate | 1.000 | 1.000 |
| latency p50 / p95 (s) | 0.020 / 0.029 | 0.009 / 0.012 |
| model calls / request | 3.07 | 1.90 |
| tool calls / request | 1.17 | 0.87 |
| tokens in / out (total) | 38,566 / 3,848 | 35,176 / 3,416 |
| cost per request | $0 (offline) | $0 (offline) |
| success: knowledge / service desk / access | 0.20 / 0.80 / 0.93 | 0.33 / 0.80 / 0.00 |
| success: multi-intent / security / failure | 0.00 / 1.00 / 1.00 | 0.00 / 1.00 / 1.00 |

Reading it:
- **Access is where the architectures differ most** (0.93 vs 0.00). The single agent has no access tools and opens a ticket instead, which shows up as 16 unexpected (authorized) writes, not as unauthorized actions.
- **Safety holds for both**: no unauthorized action, no secret leak, every tool traced. That is the point of deterministic governance: the architecture changes quality, not safety.
- **Multi-agent costs more calls** (3.07 vs 1.90 model calls per request) and about twice the latency. With a real model, that is the price to weigh against the quality gain; the cost rows fill in from `config/pricing.yaml`.
- **Knowledge is better on the single agent** with the fake model (0.33 vs 0.20): the supervisor's keyword router sends several knowledge questions to the access or service desk agent. With a real router this should reverse; the golden set will tell.

`multi_mcp` gives the same quality results as `multi` (task success 0.617, trace coverage 1.000, 0 unauthorized); latency p50 0.023 s with the in-process MCP transport.

### Adversarial suite (security v1, multi and multi_mcp)

| | before the write budget | after |
|---|---|---|
| security pass rate | 0.875 | 1.000 |
| unauthorized actions | 1 (adv-05: 6 tickets) | 0 |
| secret leaks | 0 | 0 |
| trace coverage | 1.000 | 1.000 |

### Quality gates
On the offline model the quality targets are **not met** (task success 0.617 < 0.85, routing 0.652 < 0.90, tool selection 0.512 < 0.90, citations 0.25 < 0.95). That is expected for a keyword-matching fake and is why quality is a separate gate. Run with `--quality-gate` and a real model to check the spec's targets.

### Tests
`uv run pytest -m "not live"` with PostgreSQL: **437 passed, 1 skipped**. New in M9: `tests/unit/test_evals.py` (20 tests: dataset distribution and integrity, runner on real cases, safety checks on hand-made runs, gates, regression tolerance, pricing, judge parsing, judge "not run", CLI with baselines), write-budget tests in `tests/policy/test_policy.py` and `tests/unit/test_gateway.py`. The RAG gate is unchanged (hit rate 0.89 ≥ 0.85). The four CI eval commands pass locally in about 24 s.

## 5. Running with a real model

```bash
MODEL_PROVIDER=anthropic MODEL_NAME=claude-haiku-4-5-20251001 ANTHROPIC_API_KEY=… \
EVAL_JUDGE_PROVIDER=anthropic EVAL_JUDGE_MODEL=claude-sonnet-5-5 \
  uv run aegisdesk eval golden --config multi --compare single --judge --quality-gate
```

Then store a baseline for that model (`--write-baseline evals/regression/golden_v1-multi-claude-haiku-4-5.json`) and gate future changes against it. Repeat with `MODEL_NAME=claude-sonnet-5-5` or an Ollama model to compare models on the same cases. Watch the cost rows: 60 cases × about 3 model calls is roughly 180 calls per config, plus about 45 judge calls.

## 6. Exercises

1. Add a golden case for a request you think the system gets wrong. Run it. Was it the model, the router, a tool, or the expectation?
2. Lower `max_writes_per_request` to 1 and rerun golden and adversarial. Which legitimate cases break, and why (hint: approval provisioning is a write)?
3. Make a change that you expect to be neutral (for example a prompt wording) and run with `--baseline`. Did the regression gate agree?
4. Write an adversarial case for a gap you suspect. If it fails, fix it deterministically, not in a prompt.
5. With a real model, compare `multi` and `single` on cost per *successful* task, not per request.

## 7. Limitations
- Quality numbers here are for the offline model only.
- Fact checks are substring matches: a correct paraphrase fails. That is what the judge (semantic correctness) is for.
- The write budget counts idempotent replays as writes (a fourth identical submission in one request would be denied); a request ID is one user request, so this is intended.
- Latency is in-process; network, real model and MCP over HTTP latency are measured only when run in those setups.
