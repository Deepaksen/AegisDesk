// Build docs/AegisDesk-High-Level-Design.docx (docx-js).
//
//   cd docs/design/build && npm install && npm run all
//
// Content reflects the repository as built through Milestone 11. Figures come from
// ../diagrams/*.png (rendered from the .mmd sources by render_diagrams.mjs); layout helpers
// are shared with the production design document (docx_kit.mjs).
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { HeadingLevel, PageBreak, Paragraph, TableOfContents, TextRun } from "docx";
import { BLUE, FONT, createKit } from "./docx_kit.mjs";

const here = dirname(fileURLToPath(import.meta.url));
const output = resolve(here, "..", "..", "AegisDesk-High-Level-Design.docx");
const { p, h1, h2, bullets, spacer, table, tableCaption, figure, add, landscape, build } = createKit({
  diagramsDir: resolve(here, "..", "diagrams"),
  headerText: "AegisDesk · High-Level Design · v1.0",
});

// ============================================================================================
// CONTENT
// ============================================================================================

// -- title page ----------------------------------------------------------------------------
add(
  new Paragraph({ spacing: { before: 2400, after: 200 }, children: [new TextRun({ text: "AegisDesk", size: 64, bold: true, color: BLUE, font: FONT })] }),
  new Paragraph({ spacing: { after: 120 }, children: [new TextRun({ text: "Enterprise Agentic AI Service Desk", size: 36, color: "2F5597" })] }),
  new Paragraph({ spacing: { after: 800 }, children: [new TextRun({ text: "High-Level Design", size: 44, color: "404040" })] }),
  p("A governed, observable, multi-agent IT service desk for the fictional Northstar Industries. Language models propose; deterministic code decides.", { run: { size: 24, color: "404040" } }),
  spacer(),
  table(
    ["Item", "Value"],
    [
      ["Document", "AegisDesk High-Level Design (HLD)"],
      ["Version", "1.0"],
      ["Date", "30 September 2026"],
      ["Status", "Draft for review"],
      ["Scope", "System as built through Milestone 11 (reliability): M0–M11"],
      ["Source of truth", "Repository `docs/` (ARCHITECTURE.md, design documents, ADRs 0001–0017) and the code under `src/aegisdesk/`"],
      ["Data", "All employees, assets, tickets, applications and documents are synthetic"],
    ],
    [0.25, 0.75],
  ),
  new Paragraph({ children: [new PageBreak()] }),
  new Paragraph({ heading: HeadingLevel.HEADING_1, children: [new TextRun("Contents")] }),
  new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }),
  p("If the table of contents is empty, right-click it in Word and choose Update Field.", { run: { italics: true, size: 18, color: "7F7F7F" } }),
);

// -- 1 introduction ---------------------------------------------------------------------------
add(
  h1("1 Introduction"),
  h2("1.1 Purpose"),
  p("This document describes the high-level design of AegisDesk: its context, conceptual and logical architecture, data and process flows, governance and security model, observability, evaluation, reliability and deployment, the design decisions behind them, and the design patterns used in the codebase. It is written for engineers, architects, security reviewers and operators who need to understand how the system is put together and why, without reading the code first."),
  h2("1.2 Scope"),
  p("The document covers the system as built on the milestone branch through Milestone 11:"),
  ...bullets([
    "**In scope:** the model layer, agents and LangGraph orchestration, typed tools and MCP servers, retrieval-augmented generation, the policy engine and action gateway, human approval, audit, observability, the evaluation framework, the HTTP API and Streamlit UI, the container stack, and the reliability mechanisms.",
    "**Out of scope:** Milestone 12 (release pipeline) beyond the current CI gates, a real identity provider (OIDC), production hosting, and the stretch goals of the specification.",
  ]),
  h2("1.3 Guiding principle"),
  p("LLMs do probabilistic reasoning; deterministic software does authentication, authorization, policy, approval enforcement, validation, business rules, persistence, audit and retries. **The LLM is never the security authority**: it proposes actions, and deterministic code decides whether they happen. Every design choice in this document follows from that principle."),
  h2("1.4 References"),
  table(
    ["Reference", "Content"],
    [
      ["`docs/ARCHITECTURE.md`", "Architecture overview and milestone progress"],
      ["`docs/AGENT_DESIGN.md`, `docs/MCP_DESIGN.md`, `docs/RAG_DESIGN.md`", "Agents, MCP servers, retrieval"],
      ["`docs/GOVERNANCE_DESIGN.md`, `docs/APPROVALS_DESIGN.md`", "Policy, gateway, audit, approvals"],
      ["`docs/OBSERVABILITY.md`, `docs/EVALUATION.md`", "Telemetry, evaluation framework"],
      ["`docs/API.md`, `docs/RUNBOOK.md`", "HTTP API contract, operations"],
      ["`docs/adr/0001`–`0017`", "Architecture decision records"],
      ["`docs/milestones/M0`–`M11`", "Per-milestone design notes with measured results"],
    ],
    [0.45, 0.55],
  ),
);

// -- 2 system overview -----------------------------------------------------------------------
add(
  h1("2 System overview"),
  h2("2.1 Problem statement"),
  p("Employees of Northstar Industries need help with IT: answers from company policies and guides, information about their own laptops and tickets, new support tickets, and access to applications. Some of those actions are sensitive (for example access to finance or production systems) and must follow company policy, including human approval. AegisDesk automates this with language-model agents while keeping every consequential decision in deterministic, auditable code."),
  h2("2.2 Goals and non-goals"),
  table(
    ["Goal", "How the design meets it"],
    [
      ["Answer IT questions from company documents, with citations", "RAG with access filtering before ranking; citations checked in code against what was retrieved"],
      ["Act on the user's own data only", "Identity comes from authentication, never from model arguments; ownership checks in every tool"],
      ["Sensitive actions only with policy and human approval", "Policy engine on every call; HIGH-risk provisioning only with approval evidence the gateway looks up"],
      ["Everything auditable and observable", "Append-only audit with write-ahead decisions; one trace per request across processes"],
      ["Measurably correct and safe", "Golden, adversarial, reliability and RAG suites with safety and regression gates in CI"],
      ["Provider-agnostic, runnable offline", "Model layer behind one interface, an allowlist, and an offline fake model as default"],
      ["Degrade safely under failure", "Classified failures, bounded retries, circuit breakers, idempotent replay, reconciliation"],
    ],
    [0.42, 0.58],
  ),
  tableCaption("Goals"),
  p("**Non-goals:** a production identity provider, real enterprise integrations (systems are simulated from seed data), autonomous actions without policy, and model-graded security decisions."),
  h2("2.3 Users and roles"),
  table(
    ["Persona (synthetic)", "Role", "Typical interactions"],
    [
      ["Aisha Khan (E1004)", "Finance employee", "VPN questions, laptop details, tickets, FinanceERP access request"],
      ["Grace Liu (E1010)", "Finance manager (approver)", "Approves or rejects her reports' access requests"],
      ["Ines Duarte (E1015)", "Security approver", "Security approval step for privileged applications"],
      ["Viktor Lindqvist (E1016)", "Data owner", "Data-owner approval step"],
      ["Lena Hoffmann (E1006)", "IT admin", "Sees all audit events; runs provisioning reconciliation"],
      ["Tom Becker (E1005)", "Contractor", "Restricted document classifications and applications"],
      ["Daniel Ortiz (E1007)", "Terminated employee", "Login refused"],
    ],
    [0.3, 0.27, 0.43],
  ),
  tableCaption("Synthetic users used throughout tests, evaluations and demos"),
  h2("2.4 System context"),
  ...figure("01-system-context", "System context: users reach AegisDesk through an authenticating gateway; AegisDesk uses LLM providers, simulated enterprise data (over MCP), the knowledge base and the observability stack", { maxH: 760 }),
  p("The **authenticating gateway** is the only source of identity: it forwards the verified employee ID in `X-Employee-Id` (ADR 0015). Model providers receive prompts and tool schemas but never identity claims or secrets. Enterprise data is reached only through tools, over MCP with per-call delegation tokens, and the knowledge base only through access-filtered retrieval."),
);

// -- 3 conceptual architecture --------------------------------------------------------------
add(
  h1("3 Conceptual architecture"),
  p("Conceptually AegisDesk is six layers. Reasoning (orchestration) sits between the experience layer and the capabilities, and **every** capability call crosses the governance layer, which is deterministic code."),
  ...figure("02-conceptual-layers", "Conceptual layers: probabilistic orchestration on top, deterministic governance between capabilities and data", { maxH: 700 }),
  table(
    ["Layer", "Responsibility", "Main components"],
    [
      ["1 Experience", "How people and programs use the system", "Streamlit UI, HTTP API (FastAPI), CLI"],
      ["2 Orchestration", "Understand the request, choose specialists, choose tools", "Router model, deterministic supervisor, Knowledge / Service Desk / Access specialists"],
      ["3 Capabilities", "Do things and know things", "Typed tools, MCP read and action servers, RAG pipeline"],
      ["4 Governance", "Decide whether things may happen, and record them", "Identity, policy engine, action gateway, approval service and workflow, audit log"],
      ["5 Data", "Durable state", "PostgreSQL (access workflow, audit, checkpoints, idempotency, pgvector), seed data, documents"],
      ["6 Cross-cutting", "Qualities of every layer", "Provider-agnostic model layer, observability, evaluation, reliability"],
    ],
    [0.18, 0.37, 0.45],
  ),
  tableCaption("Conceptual layers"),
);

// -- 4 logical architecture --------------------------------------------------------------
add(
  h1("4 Logical architecture"),
  h2("4.1 Containers"),
  ...figure("03-container-view", "Container view (docker compose): UI, API process with the runtime and agents, internal MCP process, PostgreSQL and the optional observability stack", { maxH: 800 }),
  table(
    ["Container", "Technology", "Responsibility"],
    [
      ["ui", "Streamlit, httpx", "Employee, manager and audit views; talks to the API over HTTP only"],
      ["api", "FastAPI, uvicorn, LangGraph", "Authentication, routes, SSE streaming, problem+json errors; hosts `AegisRuntime`, the supervisor graph, the gateway and the retriever"],
      ["mcp", "MCP SDK (Streamable HTTP)", "Read and action tool servers; verify delegation tokens; apply the same policy through their own gateway"],
      ["postgres", "PostgreSQL 16 + pgvector", "Access requests, approvals, granted access, audit events, idempotency records, documents and chunk embeddings, LangGraph checkpoints"],
      ["migrate", "Alembic, CLI", "One-shot: migrations, checkpoint tables, seed rows, knowledge-base ingestion"],
      ["observability profile", "OTel Collector, Tempo, Prometheus, Grafana", "Trace storage, metrics, the AegisDesk overview dashboard"],
    ],
    [0.17, 0.25, 0.58],
  ),
  tableCaption("Containers"),
  h2("4.2 Package structure and dependencies"),
  p("The Python package `aegisdesk` is organised by responsibility. Dependencies point from entry points through orchestration and capabilities to governance and data; cross-cutting packages are used by all layers. The UI is a separate client that depends only on the HTTP API."),
  ...figure("04-package-dependencies", "Package dependencies inside src/aegisdesk", { maxH: 700 }),
  h2("4.3 Module overview"),
  table(
    ["Package", "Responsibility", "Key types and files", "Since"],
    [
      ["`llm`", "Provider-agnostic model construction, allowlist, offline fake, structured output", "`build_chat_model`, `ModelAllowlist`, `ScriptedChatModel`, `LLMClient`", "M0"],
      ["`prompts`", "Versioned YAML prompts", "`load_prompt`, `prompts/*.yaml`", "M0"],
      ["`identity`", "Trusted user context, agent identities, delegation tokens", "`UserContext`, `authenticate`, `AgentIdentity`, `TokenIssuer`/`TokenVerifier`", "M1/M5"],
      ["`domain`", "Business entities, repository, access-workflow stores", "`ServiceDeskRepository`, `AccessStore`, `PgAccessStore`, `GuardedAccessStore`", "M1/M7"],
      ["`tools`", "Typed tools, executor (security boundary), local/remote runners", "`ToolSpec`, `ToolExecutor`, `RemoteToolRunner`, `ToolFactory`", "M1/M5"],
      ["`agents`", "Agent assembly: supervisor, specialists, single agent, reference loop", "`build_supervisor_agent`, `build_service_desk_graph_agent`, `check_knowledge_answer`", "M1/M4"],
      ["`graphs`", "LangGraph topologies and the threaded agent", "`build_tool_agent_graph`, `build_supervisor_graph`, `ThreadedGraphAgent`", "M2/M4"],
      ["`rag`", "Ingestion, embeddings, vector stores, retrieval, grounded answers", "`ingest_directory`, `HashingEmbedder`, `Retriever`, pgvector store", "M3"],
      ["`mcp_servers`", "Read and action MCP servers, HTTP app", "`build_servers`, `build_http_app`, catalogue", "M5"],
      ["`governance`", "Policy as data, engine, action gateway", "`PolicyEngine`, `ActionGateway`, `config/policy.yaml`", "M6"],
      ["`audit`", "Append-only audit events", "`AuditEvent`, `InMemoryAuditLog`, `PgAuditLog`", "M6"],
      ["`approvals`", "Approver rules, decisions, workflow, evidence", "`ApprovalService`, `AccessApprovalWorkflow`, `AccessApprovalVerifier`", "M7"],
      ["`persistence`", "Checkpointers, repository factory, idempotency store", "`open_checkpointer`, `build_repository`, `PgIdempotencyStore`", "M2/M7/M11"],
      ["`observability`", "Tracing, metrics, JSON logs, redaction, propagation, faults, LangSmith", "`configure_telemetry`, `RedactingSpanProcessor`, `instruments`, `faults`", "M8"],
      ["`evals`", "Golden/adversarial/reliability runner, evaluators, report, judge", "`EvalRunner`, `check_case`, `Report`, `Judge`", "M3/M9"],
      ["`runtime`", "Long-lived components and use cases (facade)", "`AegisRuntime`, `TurnResult`, activity summaries, `reconcile`", "M10"],
      ["`api`", "HTTP adapter: auth, routes, SSE, errors", "`create_app`, `current_user`, schemas", "M10"],
      ["`ui`", "HTTP client for the UI", "`ApiClient`, `parse_sse`", "M10"],
      ["`reliability`", "Circuit breakers, model guard, store errors", "`CircuitBreaker`, `ModelGuard`, `StoreUnavailableError`", "M11"],
      ["`cli`, `config`", "Command line, typed settings", "`aegisdesk …`, `Settings`", "M0+"],
    ],
    [0.17, 0.31, 0.42, 0.1],
  ),
  tableCaption("Modules of src/aegisdesk"),
);

// -- 5 agents ------------------------------------------------------------------------------
add(
  h1("5 Agent and orchestration design"),
  p("A request is handled by a **supervisor** and up to three **specialist agents**. The supervisor's only model call is routing; everything else it does is deterministic. Each specialist is a small tool-calling agent with its own prompt and a fixed set of tools."),
  table(
    ["Agent", "Purpose", "Tools (from `config/policy.yaml`)"],
    [
      ["Router (supervisor)", "Classify the request into tasks (`RoutingPlan`, structured output)", "none"],
      ["Knowledge", "Answer from documents with citations", "`search_knowledge_base`, `retrieve_document`, `request_handoff`"],
      ["Service Desk", "Assets and tickets", "`get_my_assets`, `list_my_tickets`, `get_ticket`, `create_ticket`, `add_ticket_comment`, `search_knowledge_base`, `request_handoff`"],
      ["Access", "Eligibility and access requests", "`get_employee_profile`, `list_my_access`, `get_application`, `check_access_eligibility`, `create_access_request`, `request_handoff`"],
      ["access_workflow (code)", "Provision approved access", "`provision_access` (HIGH risk, only with approval evidence)"],
    ],
    [0.2, 0.3, 0.5],
  ),
  tableCaption("Agents and their tool grants"),
  h2("5.1 Supervisor graph"),
  ...figure("05-supervisor-graph", "Supervisor graph: model-driven nodes (orange) and deterministic nodes (green); the approval pause is an interrupt in a deterministic node", { maxH: 720 }),
  ...bullets([
    "**Routing:** one structured call produces up to `max_tasks` (3) tasks; malformed output asks the user to rephrase, a model outage produces the safe unavailable answer.",
    "**Dispatch:** the supervisor runs pending tasks in order with `Command(goto=…)`. Specialists may request a handoff; the supervisor applies it only within a budget (`max_handoffs` = 2) and never twice for the same task.",
    "**Context isolation:** a specialist sees the visible conversation plus its task and runs in private state; only its answer and trajectory reach the parent thread.",
    "**Respond:** answers are combined deterministically (sections per specialist); no extra model call can add facts. The Knowledge answer is checked in code: cited documents must have been retrieved in this run.",
    "**Approval:** if access requests were created, `await_approval` pauses the thread with `interrupt()`; resuming re-reads the approval store, never the resume payload (ADR 0011).",
  ]),
  h2("5.2 Tool-agent graph (specialists and the single agent)"),
  ...figure("06-tool-agent-graph", "Tool-agent graph used by every specialist (and by the single-agent engine)", { maxH: 420 }),
  p("Limits are enforced by code: at most 6 model calls and 8 tool executions per request per agent (`AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`); a LangGraph recursion limit is a second backstop. Tools are never executed by a prebuilt LangGraph node: `run_tools` always calls the `ToolRunner`, which is the security boundary (ADR 0004)."),
);

// -- 6 data flow ---------------------------------------------------------------------------
add(
  h1("6 Data flow"),
  h2("6.1 A request end to end"),
  ...figure("07-request-sequence", "Data flow of one request that creates a ticket: authentication and idempotency at the edge, routing, a specialist's tool call through the executor, policy and write-ahead audit", { maxH: 820 }),
  p("Key properties of the flow:"),
  ...bullets([
    "Identity is established once at the edge and travels as `UserContext` in code; tool schemas have no identity fields, so the model cannot name a user.",
    "Every tool call is validated, authorized and audited **before** it runs; the outcome is audited after. Reads proceed when the audit trail is down; writes do not.",
    "The same `request_id` links the HTTP response header, audit events, JSON logs and the trace; the `trace_id` is returned to the client and stored in every audit event.",
    "Streaming (`Accept: text/event-stream`) relays safe activity lines built by code from graph node updates, then one result event with the same shape as the JSON response.",
  ]),
  h2("6.2 Retrieval-augmented generation"),
  ...figure("08-rag-pipeline", "RAG ingestion and query: access rules are applied inside the vector search, before ranking; citations are verified against what was retrieved", { maxH: 780 }),
  ...bullets([
    "Documents are Markdown with validated front matter (ID, title, version, classification, department, allowed roles). Chunks are split by heading and prefixed with the document title.",
    "Two embedders share one 768-dimension schema: a deterministic hashing embedder for tests and CI, Ollama `nomic-embed-text` for real use; an index refuses queries from a different embedder (ADR 0006).",
    "Retrieved text is treated as untrusted data, never as instructions; tool results are wrapped with an explicit note to that effect.",
    "Offline retrieval gate: hit rate 0.89 at k = 4, 0 access violations over 24 cases (CI threshold 0.85).",
  ]),
  h2("6.3 Trust boundaries"),
  ...figure("09-trust-boundaries", "Trust boundaries: user text, documents and model output are content; identity flows only through trusted code into delegation tokens", { maxH: 680 }),
);

// -- 7 process flows -------------------------------------------------------------------------
add(
  h1("7 Process flows"),
  h2("7.1 Sensitive access request with human approval"),
  ...figure("10-approval-sequence", "Access request for a sensitive application: the workflow pauses durably, the manager decides, and the original conversation resumes and provisions through the gateway", { maxH: 820 }),
  p("Approver rules are deterministic (`ApprovalService`): the requester can never approve their own request; a step names a specific approver (the requester's manager) or a role (`security_approver`, `data_owner`); one person cannot decide two steps of the same request; steps expire after `APPROVAL_TTL_HOURS`. Decisions are idempotent: repeating the same decision changes nothing, a conflicting one is refused (409). Since Milestone 11 the decision is written to the audit trail **before** the approval changes, and refused if it cannot be recorded."),
  h2("7.2 Request and approval lifecycles"),
  ...figure("11-state-machines", "State machines of an access request and an approval step (\"provisioned\" = provisioned_at is set on an approved or auto-approved request)", { maxH: 760 }),
  h2("7.3 Ticket creation, duplicates and the write budget"),
  ...figure("12-ticket-idempotency", "Ticket creation: idempotent replay of duplicate requests, the per-request write budget, and tool-level idempotency keys", { maxH: 780 }),
  p("Three independent mechanisms prevent duplicate or runaway writes: the API replays the stored response of a retried request (`Idempotency-Key`); every write tool derives an idempotency key from user, request, tool and validated arguments, so a repeated call returns the first result; and the policy denies more than three writes per request (`limits.max_writes_per_request`), a limit found necessary by the adversarial evaluation."),
);

// -- 8 governance ------------------------------------------------------------------------------
add(
  h1("8 Governance and security"),
  h2("8.1 Defence in depth"),
  ...figure("13-defence-in-depth", "Layers between a model's proposed tool call and its effect", { maxH: 780 }),
  h2("8.2 Identity"),
  ...bullets([
    "**Users:** `authenticate()` turns the gateway's employee ID into a `UserContext` (roles, department, manager) after checking the directory; unknown or terminated employees are refused (API 401).",
    "**Agents:** each specialist and the approval workflow has an `AgentIdentity` (id, version, type, environment) that the policy evaluates.",
    "**Across processes:** every MCP call carries a fresh HS256 JWT delegation token: `sub` (user and claims), RFC 8693 `act` (agent), `aud` (one server), `rid` (request ID), `jti`, 60-second expiry. Servers verify it and apply policy again (ADR 0008).",
  ]),
  h2("8.3 Policy engine and action gateway"),
  p("Policy is data (`config/policy.yaml`, validated strictly at startup, versioned by content hash) evaluated by a small deterministic Python engine (ADR 0009). The action gateway calls it on every tool call, locally and on the MCP servers, looks up approval evidence for HIGH-risk calls itself, and records the decision before the tool runs."),
  ...figure("14-policy-decision", "Policy decision: all deny reasons are collected; any deny wins; HIGH risk needs recorded approval", { maxH: 700 }),
  table(
    ["Risk", "Tools", "Rule"],
    [
      ["LOW", "knowledge search, document retrieval, handoff, all reads", "Allowed if granted to the agent and the environment allows it"],
      ["MEDIUM", "`create_ticket`, `add_ticket_comment`, `create_access_request`", "Only if listed in `authorized_writes`; counts toward the per-request write budget"],
      ["HIGH", "`provision_access`", "Only for `access_workflow`, and only with approval evidence (or an auto-approved standard application)"],
      ["Forbidden", "`direct_grant_production_admin`, `grant_access`, `delete_audit_events`", "Never, for any agent"],
    ],
    [0.14, 0.43, 0.43],
  ),
  tableCaption("Risk classes (policy wins over what tool code declares)"),
  p("**Environments:** development and test allow all classified tools; production currently allows only reads (writes are not yet approved for real integrations)."),
  h2("8.4 Audit"),
  p("Every tool call produces two append-only events linked by `call_id`: a **decision** event before the tool runs and an **outcome** event after. Events carry who (user, agent, version), what (tool, identifiers only), the policy decision, reasons and version, approval and approver IDs, outcome, latency, `request_id`, `thread_id` and `trace_id`. In PostgreSQL the table rejects UPDATE, DELETE and TRUNCATE (ADR 0010). If the decision event cannot be written, writes are denied (`audit_unavailable`): nothing happens without a record."),
  h2("8.5 Security verification"),
  ...bullets([
    "Security test suites exercise prompt injection through documents, identity manipulation, cross-user access, governance bypass, approval bypass and MCP boundary violations with a scripted malicious model.",
    "The adversarial evaluation (8 scripted attacks) and the 8 security cases of the golden set run in CI: **0 unauthorized actions**, 100% security pass rate, on in-process and MCP transports.",
    "Telemetry is checked for leaks: no user text, answers, secrets or raw employee IDs in any exported span attribute.",
  ]),
);

// -- 9 observability -----------------------------------------------------------------------
add(
  h1("9 Observability"),
  p("OpenTelemetry is the backbone (ADR 0013): one trace per request across the API, agents, tools and MCP servers (W3C `traceparent` travels in MCP `_meta`), the specification's metrics as OpenTelemetry instruments, and structured JSON logs sharing the same trace ID. LangSmith is optional and receives redacted payloads."),
  ...figure("15-observability-pipeline", "Telemetry pipeline: redaction before any exporter, a second pass in the collector, and one trace ID joining spans, logs and audit events", { maxH: 620 }),
  table(
    ["Signal", "Content", "Where it goes"],
    [
      ["Spans", "HTTP server span, `aegisdesk.request`/`resume`, `chat <model>` (tokens, latency, attempts), `invoke_agent`, `execute_tool` → `policy.evaluate` + `tool.handler`, `mcp.call` / `mcp.server`, `rag.retrieve` (document IDs only), `approval.await/decide/apply`", "Tempo (OTLP), or a span tree in the terminal (`--trace`)"],
      ["Metrics", "Requests and failures, agent invocations, LLM and tool calls, tool errors, latency histograms, tokens, approvals and rejections, policy denials, no-evidence retrievals, HTTP requests, model errors, circuit transitions, idempotent replays", "Prometheus (scraped from the API, OTLP from other processes), Grafana dashboard"],
      ["Logs", "JSON lines with `trace_id`, `span_id`, `request_id`, `thread_id`, `agent`; scrubbed like spans", "stdout"],
      ["Audit", "Decision and outcome events with `trace_id`", "PostgreSQL `audit_events`, `GET /api/v1/audit`"],
    ],
    [0.12, 0.6, 0.28],
  ),
  tableCaption("Telemetry signals"),
  h2("9.1 Privacy by construction"),
  ...bullets([
    "Content (messages, tool arguments and results, answers) is never set as a span attribute.",
    "`RedactingSpanProcessor` keeps only allowlisted attribute keys, scrubs secret patterns (API keys, JWTs, bearer tokens) and caps lengths; users appear as a pseudonymous hash.",
    "Metric labels are low-cardinality (agent, tool, model, status, category, route template); a user is never a label.",
    "The collector deletes `gen_ai.prompt*` and `gen_ai.completion*` attributes again, as a second layer.",
  ]),
  p("**Fault injection** (`AEGIS_FAULTS`) triggers failures at the real seams (tool errors and timeouts, MCP outages, retrieval errors, model timeouts and malformed output, database errors) so that debugging from telemetry can be practised and every failure path is tested."),
);

// -- 10 evaluation -----------------------------------------------------------------------------
add(
  h1("10 Evaluation"),
  p("Evaluation is deterministic first (ADR 0014): code checks what a run **did** (its trajectory, audit events, spans, logs and data changes); an optional LLM judge grades only what code cannot (completeness, clarity, semantic correctness, groundedness, helpfulness) and never gates."),
  ...figure("16-evaluation-lifecycle", "Evaluation lifecycle: versioned datasets, a runner that rebuilds a clean world per case, deterministic checks, and three kinds of gate", { maxH: 700 }),
  table(
    ["Suite", "Cases", "What it proves", "Result (offline model)"],
    [
      ["Golden v1", "60 (15 knowledge, 10 service desk, 15 access, 8 multi-intent, 8 security, 4 failure)", "Routing, tool choice, policy, approval, facts, citations, effects", "Task success 0.617; 0 unauthorized; trace coverage 1.0"],
      ["Adversarial v1", "8 scripted attacks", "No unauthorized action whatever the model outputs", "1.000 pass; 0 unauthorized"],
      ["Reliability v1", "13 injected failures", "Safe degradation for every failure type", "1.000 pass; 0 unexpected writes"],
      ["RAG retrieval v1", "24", "Retrieval quality and access filtering", "Hit rate 0.89; 0 access violations"],
    ],
    [0.17, 0.3, 0.3, 0.23],
  ),
  tableCaption("Evaluation suites"),
  table(
    ["Gate", "Condition", "Where"],
    [
      ["Safety", "0 unauthorized actions; approval enforced 100%; trace coverage 100%; 0 secret leaks; security cases 100%", "CI, every suite, multi and multi_mcp"],
      ["Regression", "No quality metric below the committed baseline (per dataset, configuration and model)", "CI"],
      ["Quality", "Routing ≥ 0.90, tool selection ≥ 0.90, task success ≥ 0.85, citations ≥ 0.95", "`--quality-gate` with a real model"],
    ],
    [0.14, 0.6, 0.26],
  ),
  tableCaption("Gates"),
  p("The quality targets are not met by the offline fake model (it routes by keyword), which is why quality is a separate gate; the numbers above describe architectures and safety, not a real model. The comparison of the multi-agent and single-agent designs on the same cases showed equal safety (0 unauthorized actions for both) and much better access handling for multi-agent (0.93 vs 0.00) at about 1.6× the model calls per request."),
);

// -- 11 reliability ----------------------------------------------------------------------------
add(
  h1("11 Reliability"),
  p("Every failure listed by the specification is injected at its real seam and degrades to a safe, explained result (ADR 0017). Nothing is retried that is not safe to retry, and nothing is written without an audit record."),
  ...figure("17-model-guard", "Guarded model call: classification, bounded retries with backoff and jitter, circuit breaker, and the safe outcome", { maxH: 640 }),
  ...figure("20-circuit-breaker", "Circuit breaker states (one breaker per model and per MCP server, per process)", { maxH: 300 }),
  table(
    ["Failure", "Behaviour"],
    [
      ["Model timeout / outage / rate limit", "Retried with backoff (SDK retries off: one policy); then a fixed \"temporarily unavailable\" answer, `stop_reason = model_error`, API 503 + `Retry-After`; the breaker then fails fast (`circuit_open`)"],
      ["Malformed model output", "Unparsable routing → ask to rephrase; broken tool-call JSON → answered as a traced `malformed_tool_call` so the model can correct it; empty reply → fixed answer"],
      ["Database error", "`StoreUnavailableError` → tool reports `unavailable` / API 503; audit outage refuses writes and approval decisions; authorization fails closed"],
      ["MCP timeout / outage", "Read tools retried once, writes never; per-server breaker"],
      ["Tool failure", "Error category returned to the model; approved-but-unprovisioned requests finished by `aegisdesk approvals reconcile`"],
      ["Duplicate request", "Idempotent replay of the stored response; 409 while in flight; 422 when a key is reused for another message"],
    ],
    [0.28, 0.72],
  ),
  tableCaption("Failure handling"),
  p("Measured live: with the router model forced to time out, the first two requests returned 503 `model_timeout` after about 1.25 s of retries; the breaker then opened and the third returned 503 `circuit_open` in 20 ms, while `/ready` stayed ready and reported the open circuit."),
);

// -- 12 deployment -----------------------------------------------------------------------------
add(
  h1("12 Deployment view"),
  ...figure("18-deployment", "docker compose: startup order enforced by health conditions; the observability stack is an optional profile", { maxH: 640 }),
  ...bullets([
    "One image (`Dockerfile`: Python 3.12 slim, `uv sync --locked`, non-root user); the UI image adds the `ui` dependency group.",
    "`migrate` runs migrations (Alembic 0001–0004), creates checkpoint tables, loads seed rows and ingests documents, then exits; nothing creates schema at application startup.",
    "`mcp` is on the internal network only; the API is its only client and authenticates each call with a delegation token signed with `MCP_TOKEN_SECRET` (32+ characters, from `.env`).",
    "`api` health check is `/ready` (database and checkpointer reachable); open circuit breakers are reported but do not make the service unready.",
    "Configuration is environment-only (`Settings`, pydantic-settings); secrets never appear in prompts, images or telemetry.",
  ]),
  p("The compose file is validated in CI (`docker compose config`); in the development environment used to build the milestones no Docker daemon was available, so `docker compose up` itself has not been exercised there. The same processes were run directly for the API and UI walkthroughs."),
);

// -- 13 data model -----------------------------------------------------------------------------
landscape(
  h1("13 Data model", false),
  ...figure("19-data-model", "Main tables (PostgreSQL); LangGraph checkpoint tables and the rag_index table are omitted", { maxW: 930, maxH: 430 }),
);
add(
  table(
    ["Store", "Memory implementation", "PostgreSQL implementation", "Selected by"],
    [
      ["Access workflow (requests, approvals, grants)", "`InMemoryAccessStore`", "`PgAccessStore` (migration 0003)", "`DATA_STORE`"],
      ["Audit events", "`InMemoryAuditLog`", "`PgAuditLog`, append-only (migration 0002)", "`AUDIT_STORE`"],
      ["Conversation checkpoints", "SQLite file (default)", "LangGraph `PostgresSaver`", "`CHECKPOINT_STORE`"],
      ["Idempotency records", "`InMemoryIdempotencyStore`", "`PgIdempotencyStore` (migration 0004)", "`DATA_STORE`"],
      ["Knowledge index", "`InMemoryVectorStore`", "pgvector, HNSW, cosine (migration 0001)", "`VECTOR_STORE`"],
      ["Reference data (employees, assets, tickets, applications)", "Seed JSON", "Seed JSON (tickets and assets stay in-process)", "—"],
    ],
    [0.25, 0.26, 0.33, 0.16],
  ),
  tableCaption("Stores and their implementations (each pair shares a contract test suite)"),
  p("State changes are single conditional statements (decide only if pending; provision only if `provisioned_at IS NULL`; create with `ON CONFLICT (idempotency_key) DO NOTHING`), so concurrent API replicas cannot double-decide or double-grant (ADR 0012)."),
);

// -- 14 decisions --------------------------------------------------------------------------------
const adrs = [
  ["0001", "Record decisions as ADRs", "Architectural reasoning is part of the learning goal; decisions stay reviewable", "Wiki pages, commit messages"],
  ["0002", "Provider-agnostic model layer, allowlist, offline fake", "No provider lock-in; reviewed model list; runs without secrets", "Direct SDK calls; unrestricted model names"],
  ["0003", "Tools take identity from trusted context", "The model cannot name a user; ownership checks cannot be bypassed by arguments", "Identity parameters validated per tool"],
  ["0004", "LangGraph with our own nodes", "Explicit state, checkpointing, interrupts, streaming, while tool execution stays in our security boundary", "Prebuilt ReAct agent; hand-written loop only"],
  ["0005", "PostgreSQL + pgvector behind an interface", "One database for relational data and vectors; access filter in SQL before ranking", "Dedicated vector database"],
  ["0006", "Ollama embeddings, hashing embedder for tests", "Real semantics locally; deterministic offline tests; same schema", "Hosted embedding API"],
  ["0007", "Specialised agents behind a deterministic supervisor", "Least privilege per agent; routing is the only model decision in the supervisor", "One agent with all tools; LLM supervisor with tools"],
  ["0008", "Two MCP servers with per-call delegation tokens", "Standard tool protocol; read/write separation; identity and agent carried and verified per call", "Direct function calls; long-lived API keys"],
  ["0009", "Deterministic policy engine in Python, policy as data", "Transparent, testable, fail-closed; structured like Rego deny sets so OPA could replace the engine", "OPA/Rego sidecar now; checks scattered in tools"],
  ["0010", "Append-only audit in PostgreSQL", "Tamper resistance by database rules; decision before action", "Application logs as audit"],
  ["0011", "Approval as a graph interrupt, store as source of truth", "Durable pause; resume cannot be spoofed by its payload; provisioning only by the workflow identity", "Polling; model-mediated approval"],
  ["0012", "PostgreSQL for workflow state", "Approvals survive restarts and span processes; conditional updates prevent races", "In-memory state only"],
  ["0013", "OpenTelemetry backbone, LangSmith optional, redaction by allowlist", "Vendor-neutral, one trace across processes, privacy by construction", "LangSmith only; denylist redaction"],
  ["0014", "Deterministic-first evaluation, split gates, scripted attacks, opt-in judge", "Safety provable in CI without a model; quality measured separately", "LLM-judged everything"],
  ["0015", "FastAPI thin adapter over a shared runtime; trusted-header identity", "One code path for CLI and API; identity in one place; OIDC is a one-file change", "Logic in routes; API-issued tokens"],
  ["0016", "Streamlit for the first UI", "Minimal effort, forces an HTTP-only client, testable headless", "React/Next.js now"],
  ["0017", "Resilience by safe degradation", "One retry policy, circuit breakers, idempotent replay, write-ahead audit, reconciliation", "Fallback model; server-side write retries"],
];
add(
  h1("14 Design decisions and rationale"),
  p("Significant decisions are recorded as ADRs in `docs/adr/`. The table summarises each decision, its main rationale and the alternatives that were rejected."),
  table(["ADR", "Decision", "Rationale", "Alternatives rejected"], adrs, [0.085, 0.29, 0.375, 0.25]),
  tableCaption("Architecture decision records"),
  h2("14.1 Cross-cutting decisions"),
  ...bullets([
    "**Deterministic core, model at the edge.** Models classify, choose tools and write answers; code validates, authorizes, approves, persists, audits and retries.",
    "**Fail closed.** Unknown tools, agents, environments or errors in the policy engine deny; an unreadable audit trail or approval store stops writes.",
    "**Least privilege everywhere.** Per-agent tool sets, read/action server separation, per-call token audiences, a workflow-only provisioning identity.",
    "**Everything has an offline twin.** Fake model, hashing embedder, in-memory stores and in-process MCP make the whole system runnable and testable in CI without secrets or services.",
    "**Measured, not asserted.** Every milestone added tests and, from M9, evaluation gates; several real defects were found by those suites (write bursts, untraced refusals, untraced malformed calls, MCP server store wiring).",
  ]),
);

// -- 15 patterns ---------------------------------------------------------------------------------
const patterns = [
  ["Policy Enforcement Point / Gateway", "`governance/gateway.py` `ActionGateway`", "One mandatory choke point for authorization and audit on every tool call, host and MCP server alike"],
  ["Policy as data (Policy Decision Point)", "`config/policy.yaml`, `governance/policy.py`", "Rules reviewed and versioned separately from code; engine is small and testable"],
  ["Strategy (via Protocols)", "`ToolRunner`: `ToolExecutor`, `RemoteToolRunner`, `CompositeToolRunner`; `VectorStore`; `Embedder`", "Swap local/MCP transport, stores and embedders without touching callers"],
  ["Factory", "`build_chat_model`, `ToolFactory.from_settings`, `build_gateway`, `build_repository`, `build_idempotency_store`", "Configuration decides implementations in one place"],
  ["Facade", "`runtime.AegisRuntime`", "A small use-case API over many components, shared by API and CLI"],
  ["Adapter (thin API)", "`api/app.py`, `mcp_servers/server.py`", "Map protocols (HTTP, MCP) onto the same core without duplicating logic"],
  ["Repository / Store abstraction", "`ServiceDeskRepository`, `AccessStore`, `AuditLog`, `IdempotencyStore`", "Memory and PostgreSQL implementations behind one contract, verified by shared tests"],
  ["Supervisor–worker orchestration", "`graphs/supervisor_graph.py`", "Specialists with least privilege; deterministic dispatch and combination"],
  ["Explicit state machine", "LangGraph graphs; access-request and approval statuses", "Every step named, checkpointed and streamable; transitions testable"],
  ["Human-in-the-loop interrupt/resume", "`await_approval` + `ApprovalService.decide` + `resume`", "Durable, auditable pause of a long-running workflow"],
  ["Proxy / Decorator", "`GuardedAccessStore`, `RedactingSpanProcessor`", "Add outage translation and redaction transparently around existing objects"],
  ["Circuit Breaker", "`reliability/breaker.py`", "Fail fast during outages, recover automatically"],
  ["Retry with exponential backoff and jitter", "`reliability/model_guard.py`", "Absorb transient model failures without synchronized retries"],
  ["Idempotency Key / idempotent receiver", "tool `idempotency_key`, `persistence/idempotency.py`", "Retries never duplicate writes or re-run requests"],
  ["Write-ahead audit (append-only log)", "gateway decision events, `ApprovalService` decision events", "Never act without a record; tamper-resistant history"],
  ["Reconciliation loop", "`AegisRuntime.reconcile`", "Converge approved-but-unprovisioned state after partial failures"],
  ["Registry / catalogue", "breaker registry, metrics `CATALOGUE`, MCP tool catalogue", "One authoritative list used by code, docs and tests"],
  ["Template (versioned prompts)", "`prompts/*.yaml`, `load_prompt`", "Prompts reviewed and versioned; every call records its prompt version"],
  ["Dependency injection", "app factory `create_app`, FastAPI `Depends`, builder parameters", "Tests inject fakes (model, stores, runtime) without patching"],
  ["Null object / offline fake", "`ScriptedChatModel`, `HashingEmbedder`, in-process MCP", "Full system behaviour without external services"],
];
add(
  h1("15 Design patterns in the codebase"),
  table(["Pattern", "Where", "Why"], patterns, [0.26, 0.37, 0.37]),
  tableCaption("Design patterns"),
);

// -- 16 quality attributes -----------------------------------------------------------------------
add(
  h1("16 Quality attributes"),
  table(
    ["Attribute", "Approach", "Evidence"],
    [
      ["Security", "Trusted identity, least privilege, deterministic policy, approval evidence, write-ahead audit, redaction", "Security test suites; adversarial suite 0 unauthorized actions"],
      ["Reliability", "Classified failures, retries only where safe, breakers, idempotent replay, reconciliation", "Reliability suite 13/13; live outage demonstration"],
      ["Observability", "One trace per request, §24 metrics, JSON logs, trace IDs in audit", "Trace coverage 1.0 gate; telemetry tests"],
      ["Correctness", "Structured outputs, schema validation, code-checked citations", "Golden suite; RAG gate"],
      ["Maintainability", "Packages by responsibility, protocols, small modules, ADRs", "Strict mypy, ruff; 20 packages with single responsibilities"],
      ["Testability", "Offline twins for every dependency, dependency injection", "522 tests (with PostgreSQL), deterministic in CI"],
      ["Portability", "Provider-agnostic model layer, environment configuration, containers", "Anthropic, Ollama and fake models; compose stack"],
      ["Performance", "Deterministic dispatch (one routing call), bounded loops, fail-fast breakers", "Latency and token metrics per request in evaluations"],
    ],
    [0.19, 0.48, 0.33],
  ),
  tableCaption("Quality attributes"),
);

// -- 17 risks ------------------------------------------------------------------------------------
add(
  h1("17 Risks, limitations and roadmap"),
  table(
    ["Risk / limitation", "Impact", "Mitigation / next step"],
    [
      ["Trusted-header identity", "Anyone reaching the API port could impersonate a user", "Deploy only behind an authenticating gateway; replace `current_user` with OIDC verification"],
      ["Quality measured only with the offline model", "Real-model routing and answer quality unknown in this repository", "Run the golden suite with `--quality-gate --judge` and commit real-model baselines"],
      ["Circuit breakers are per process", "Each replica learns about an outage on its own", "Acceptable at this scale; shared state (e.g. Redis) if needed"],
      ["Compose stack not run in the build environment", "Container-level issues could surface on first `docker compose up`", "CI validates compose; M12 adds image builds and a smoke test"],
      ["Python policy engine instead of OPA", "Differs from the specification's suggested technology", "Deny-set structure mirrors Rego; the engine is replaceable behind `PolicyEngine`"],
      ["Turn that ended in 503 already wrote the user message", "Retry adds the message again to the thread", "Visible but harmless; a transactional thread write would remove it"],
    ],
    [0.28, 0.32, 0.4],
  ),
  tableCaption("Risks and limitations"),
  p("**Roadmap:** Milestone 12 turns the existing tests and evaluation gates into a release pipeline (image builds, stack smoke tests, promotion rules). Stretch goals from the specification (hybrid retrieval and reranking, an agent and tool registry, cost budgets, canary and shadow evaluation, OIDC, Kubernetes) build on the extension points described here."),
);

// -- appendices -----------------------------------------------------------------------------------
add(
  h1("Appendix A: Configuration reference"),
  table(
    ["Area", "Settings"],
    [
      ["Model", "`MODEL_PROVIDER`, `MODEL_NAME`, `MODEL_TEMPERATURE`, `MODEL_MAX_TOKENS`, `MODEL_TIMEOUT_SECONDS`, `MODEL_MAX_RETRIES`, `MODEL_RETRY_BACKOFF_SECONDS`, `ANTHROPIC_API_KEY`, `OLLAMA_BASE_URL`"],
      ["Agents", "`AGENT_MAX_STEPS`, `AGENT_MAX_TOOL_CALLS`, `AGENT_MAX_HANDOFFS`"],
      ["RAG", "`EMBEDDING_PROVIDER`, `EMBEDDING_MODEL`, `VECTOR_STORE`, `RAG_TOP_K`, `RAG_MIN_SCORE`"],
      ["Tools / MCP", "`TOOL_TRANSPORT` (local, mcp_inprocess, mcp_http), `MCP_TOKEN_SECRET`, `MCP_READ_URL`, `MCP_ACTION_URL`, `MCP_TIMEOUT_SECONDS`"],
      ["Governance", "`POLICY_PATH`, `AUDIT_STORE`, `AEGIS_ENV`"],
      ["Workflow state", "`DATA_STORE`, `CHECKPOINT_STORE`, `DATABASE_URL`, `APPROVAL_TTL_HOURS`"],
      ["Observability", "`TELEMETRY_EXPORTER`, `OTEL_EXPORTER_OTLP_ENDPOINT`, `LOG_FORMAT`, `LOG_LEVEL`, `LANGSMITH_*`, `AEGIS_FAULTS`"],
      ["Reliability", "`BREAKER_FAILURE_THRESHOLD`, `BREAKER_RESET_SECONDS`, `IDEMPOTENCY_TTL_HOURS`, `IDEMPOTENCY_STALE_SECONDS`"],
      ["Evaluation / UI", "`EVAL_JUDGE_PROVIDER`, `EVAL_JUDGE_MODEL`, `AEGIS_API_URL`, `GRAFANA_URL`"],
    ],
    [0.2, 0.8],
  ),
  h1("Appendix B: HTTP API summary"),
  table(
    ["Endpoint", "Purpose"],
    [
      ["`GET /health`, `GET /ready`, `GET /metrics`", "Liveness, readiness (with circuit states), Prometheus metrics"],
      ["`GET /api/v1/me`", "Caller identity as resolved by the API"],
      ["`POST /api/v1/threads`", "Create a conversation"],
      ["`POST /api/v1/threads/{id}/messages`", "One turn; JSON or SSE; `Idempotency-Key` for replay"],
      ["`GET /api/v1/threads/{id}`", "Conversation (user and assistant messages) and pending approvals"],
      ["`GET /api/v1/approvals`, `GET /api/v1/approvals/{id}`", "Approvals waiting for the caller; one approval"],
      ["`POST /api/v1/approvals/{id}/approve` · `/reject`", "Decide a step; resumes the workflow when settled"],
      ["`GET /api/v1/audit`", "Audit events (own; auditors see all)"],
    ],
    [0.45, 0.55],
  ),
  h1("Appendix C: Glossary"),
  table(
    ["Term", "Meaning"],
    [
      ["Action gateway", "The component that authorizes, audits and records every tool call"],
      ["Approval evidence", "Proof, looked up by the gateway in the store, that all approval steps of a request were approved"],
      ["Checkpoint", "Persisted LangGraph state of a conversation thread after each node"],
      ["Delegation token", "Short-lived JWT carrying user, acting agent, audience and request ID on each MCP call"],
      ["Golden dataset", "Versioned set of requests with the behaviour they must produce"],
      ["Interrupt / resume", "LangGraph mechanism to pause a thread durably and continue it later"],
      ["MCP", "Model Context Protocol, the tool protocol between the host and the tool servers"],
      ["Specialist", "A tool-calling agent with a narrow purpose and a fixed tool set"],
      ["Trajectory", "The recorded steps of a run: routing, model calls, tool calls, specialist results"],
    ],
    [0.25, 0.75],
  ),
);
const stats = await build(output, {
  title: "AegisDesk High-Level Design",
  description: "High-level design of the AegisDesk agentic service desk (M0–M11)",
});
console.log(`wrote ${output} (${stats.figures} figures, ${stats.tables} captioned tables, ${stats.sections} sections)`);
