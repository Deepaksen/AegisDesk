# AegisDesk runbook

How to start, check, operate and troubleshoot the platform (spec §41). All data is synthetic.

## Start

### Everything in containers

```bash
cp .env.example .env
# set MCP_TOKEN_SECRET to 32+ random characters, e.g.
python -c "import secrets; print(secrets.token_urlsafe(48))"
docker compose up --build                          # postgres, migrate, mcp, api, ui
docker compose --profile observability up --build  # + collector, Tempo, Prometheus, Grafana
```

| Service | URL | Notes |
|---|---|---|
| UI | http://localhost:8501 | Streamlit; pick a synthetic employee in the sidebar |
| API | http://localhost:8000/docs | OpenAPI; `X-Employee-Id` header |
| Grafana | http://localhost:3000 | with the observability profile; set `TELEMETRY_EXPORTER=otlp` in `.env` |
| Prometheus | http://localhost:9090 | scrapes the collector and `api:8000/metrics` |
| MCP servers | internal only (`mcp:8765`) | the API is their only client |

Startup order is enforced: `postgres` (healthy) → `migrate` (migrations, checkpoint tables, seed rows, knowledge index; must exit 0) → `mcp` (healthy) → `api` (ready) → `ui`.

### Locally, without Docker

```bash
uv sync
uv run aegisdesk api serve                          # terminal 1 (memory stores, local tools)
uv run streamlit run apps/ui/streamlit_app.py       # terminal 2
```

With `DATA_STORE=memory`, requests and approvals live inside the API process. Approve through the API or UI, not a separate CLI process.

## Check

```bash
curl -s localhost:8000/health        # {"status":"ok"}               liveness
curl -s localhost:8000/ready         # {"status":"ready","database":"ok",...}  readiness (503 otherwise)
curl -s localhost:8000/metrics | grep aegisdesk_requests_total
docker compose ps                    # health of each service
docker compose logs api --since 10m  # JSON logs: trace_id, request_id, thread_id on every line
```

## Operate

| Task | How |
|---|---|
| Walk the §43 flow | [M10 notes](milestones/M10-api-ui.md#3-the-spec-section-43-walkthrough-with-curl), or the UI |
| Approve from the command line | `docker compose exec api aegisdesk approvals list --as E1010`, then `... approvals approve AP-0001 --as E1010 --comment ok` (resumes the thread) |
| Inspect audit events | `GET /api/v1/audit?request_id=…` (own events; IT admin sees all), or `docker compose exec api aegisdesk audit --user E1004` |
| Follow one request | take `trace_id` from the response or the UI, open it in Grafana Explore (Tempo) |
| Run the evaluations | `uv run aegisdesk eval golden --config multi --compare single`; adversarial: `--dataset evals/adversarial/security_v1.yaml` |
| Re-seed access data | `docker compose run --rm migrate` (idempotent: never overwrites existing rows) |
| Re-index documents | `docker compose exec api aegisdesk rag ingest` (unchanged documents are skipped) |
| Reset everything | `docker compose down -v` (deletes the database volume) |

## Troubleshoot

| Symptom | Likely cause | What to do |
|---|---|---|
| `mcp` or `api` exits at start with "set MCP_TOKEN_SECRET" / "token secret must be at least 32 characters" | secret missing or short in `.env` | set it; both services must share it |
| `migrate` fails | database not reachable, or an old volume with a different schema | `docker compose logs migrate`; as a last resort `docker compose down -v` |
| `/ready` is 503 with `database: unavailable` | PostgreSQL down or `DATABASE_URL` wrong | `docker compose ps postgres`, check the URL |
| 401 from every API call | no `X-Employee-Id`, or an unknown or terminated employee (`E1007` is terminated) | send a valid synthetic ID |
| Tool calls fail with `unavailable` or `timeout` | MCP servers down or slow | `docker compose logs mcp`; the trace shows which `mcp.call` failed; reads were retried, writes never are |
| Manager sees no approvals | wrong approver (e.g. E1011 for E1004's request), or it expired (`APPROVAL_TTL_HOURS`) | check `GET /api/v1/approvals/{id}` as the requester |
| Approval accepted but the conversation did not continue | thread not paused (already resumed), or the request still waits for another step | the decision response has `note`; see `pending_approvals` in the thread |
| A write was denied with `write_budget_exceeded` | more than `limits.max_writes_per_request` writes in one request | expected for runaway loops; start a new request |
| Answers look like raw JSON | the offline fake model (`MODEL_PROVIDER=fake`) echoes tool results | configure a real model (README) |
| UI says "API unreachable" | wrong API URL in the sidebar, or the API is down | `AEGIS_API_URL`, `curl /health` |

## Configuration that matters in production

`AEGIS_ENV=production` makes the policy read-only for now (M6). A production deployment also needs:
- a real identity provider in front of the API, with the trusted header stripped from client requests (ADR 0015);
- secrets from a secret manager, not `.env`;
- TLS;
- backups of the PostgreSQL volume.

These are deliberately out of scope for this learning platform.
