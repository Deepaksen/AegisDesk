// Sections 22–27: the GenX platform, roadmap, decisions, risks, glossary and sources.
export function platform(k) {
  const { p, h1, h2, bullets, numbered, table, tableCaption, callout, code, figure, add } = k;

  // -- 22 GenX ---------------------------------------------------------------------------------
  add(
    h1("22 Phase 3: the GenX platform"),
    h2("22.1 In plain words"),
    ...callout("plain", [
      "A **paved road**. Today every team that wants an agent has to solve identity, policy, approvals, audit, memory, evaluation, observability, cost and deployment on its own; AegisDesk needed eleven milestones to do it once. GenX turns those solutions, and the integrations of Phase 2, into shared and supported components. A new use case then writes only what is unique to it: its prompts, its domain tools, its rules and its test data.",
    ]),
    h2("22.2 What makes a component reusable"),
    ...bullets([
      "It is needed by at least two use cases, not just AegisDesk.",
      "It has a **stable contract** (a port) that use cases program against; adapters behind it can change.",
      "It is **configured by data** (a manifest, a policy file, a dataset), not by forking code.",
      "It is owned by the platform team with its own SLOs, evaluations and runbook.",
      "It is safe with **many tenants**: tenant-scoped keys, quotas, audit partitions and telemetry.",
    ]),
    h2("22.3 Capability map"),
    ...figure("31-genx-capability-map", "GenX capability map: seven layers of reusable capabilities", { maxH: 700 }),
    h2("22.4 Components extracted from AegisDesk"),
    table(
      ["GenX component", "Origin in AegisDesk", "What must be generalised", "Integrations behind it"],
      [
        ["Model gateway and guard", "`llm/factory.py`, `llm/allowlist.py`, `reliability/model_guard.py`, `reliability/breaker.py`", "Allowlists, budgets and fallbacks per use case", "Vertex AI, Bedrock, Foundry; Apigee quotas; AI Guard"],
        ["Policy engine and gateway", "`governance/policy.py`, `governance/gateway.py`, `config/policy.yaml`", "Rule schema per use case, autonomy levels, dry run, compensation declarations", "Credo AI (intent), Apigee (coarse policy)"],
        ["Identity and delegation", "`identity/context.py`, `identity/tokens.py`, `identity/agent.py`, `api/auth.py`", "OIDC, token exchange, agent identities", "Okta, Entra Agent ID"],
        ["Approval workflows", "`approvals/service.py`, `approvals/workflow.py`, `approvals/evidence.py`", "Configurable approval routes as Temporal workflow templates", "Temporal, Teams approval cards"],
        ["Audit service", "`audit/events.py`, `audit/postgres.py`", "Tenant partitions, WORM export, retention per use case", "Immutable storage, Rubrik"],
        ["Tool SDK and MCP server template", "`tools/base.py`, `tools/executor.py`, `tools/remote.py`, `mcp_servers/`", "Token verification, idempotency and compensation built into a server template", "Apigee MCP gateway, API hub"],
        ["RAG service", "`rag/`", "Several corpora, tenant access filters", "pgvector or managed vector search"],
        ["Context and memory service", "New in Phase 2 (§8)", "—", "Foundry, AgentCore, Memory Bank"],
        ["Prompt registry", "`prompts/loader.py` and the prompt files", "Versions, labels, publication", "Cloud prompt stores, Arize experiments"],
        ["Agent templates", "`agents/`, `graphs/` (supervisor, tool-calling loop)", "Parameterised templates", "LangGraph"],
        ["Observability SDK", "`observability/` (tracing, redaction, metrics, logging)", "OpenInference attributes, cost and tenant labels", "Arize, OTel backends"],
        ["Evaluation harness and gates", "`evals/` package, the datasets under `evals/`, CI gates", "Dataset format and gate configuration per use case", "Arize experiments, Zscaler red teaming"],
        ["Reliability kit", "`reliability/`, `persistence/idempotency.py`", "Shared as is", "—"],
        ["API and UI kit", "`api/app.py`, `runtime.py`, `ui/client.py`, `apps/ui/`", "Activity stream, SSE, problem+json, approvals inbox as components", "Teams, Gemini Enterprise, A2A"],
        ["Delivery templates", "CI workflows, `Dockerfile`, compose", "Reusable pipelines, Terraform cell modules", "Argo CD, Lyzr, Credo AI gate"],
      ],
      [0.19, 0.31, 0.26, 0.24],
    ),
    tableCaption("GenX components and where they come from"),
    h2("22.5 Reference architecture"),
    ...figure("32-genx-reference", "GenX reference architecture: a control plane, shared services in every cell, use-case workloads on top", { maxH: 560 }),
    p("The **control plane** is where teams build and govern: a developer portal with templates and onboarding, the registries (Lyzr, API hub, Credo AI), policy and configuration bundles, and the release pipeline. The **data plane** is the set of cells from Phase 1, now hosting **shared services** (model gateway, context and memory, RAG, policy gateway and audit, workflow workers) and the **use-case workloads** that call them through the GenX SDK. The operate layer is shared, with every signal labelled by use case."),
    h2("22.6 Tenancy and isolation"),
    p("Each use case is a **tenant**. It gets a Kubernetes namespace per cell, a Temporal namespace, an Apigee API product and environment, a Lyzr project, a Credo AI use case, an Arize project and a FinOps cost centre label. The isolation level follows the risk tier:"),
    table(
      ["Tier", "Intended for", "Isolation"],
      [
        ["Shared", "Low-risk internal assistants", "Namespaces in shared cells; multi-tenant shared services with tenant-scoped keys, quotas and audit partitions"],
        ["Dedicated services", "Medium risk or sensitive data", "Own database, memory store and Temporal namespace; shared gateways and control plane"],
        ["Dedicated cell", "High risk, regulated data, or data residency", "Own cells and keys; only the control plane is shared"],
      ],
      [0.18, 0.3, 0.52],
    ),
    tableCaption("Isolation tiers"),
    h2("22.7 Onboarding a new use case"),
    ...figure("33-genx-onboarding", "The path of a new use case from idea to production on GenX", { maxH: 680 }),
    ...numbered([
      "**Intake:** register the idea in Credo AI; the AI governance office sets the risk tier and required controls.",
      "**Scaffold:** generate a repository from a GenX template (agents, manifest, pipeline, datasets skeleton).",
      "**Register:** create the agents in Lyzr and their identities in Okta or Entra, each with an owner.",
      "**Tools:** reuse tools from the API hub or publish new MCP servers built from the template.",
      "**Test data:** write golden, adversarial and reliability datasets; these are the use case’s definition of “works”.",
      "**Assure:** pass the evaluation gates, red teaming and the evidence required by the policy packs.",
      "**Go live:** approval by the use-case owner and the AI governance office, then deployment through the standard pipeline with SLOs, budgets and the decision risk index from day one.",
    ]),
    h2("22.8 AegisDesk on GenX"),
    ...figure("34-aegisdesk-on-genx", "AegisDesk on GenX: the use case keeps what is unique to it and declares the rest in a manifest", { maxH: 520 }),
    p("In Phase 3 AegisDesk is re-platformed: the model layer, gateway, audit, approval, observability, evaluation-runner and reliability code moves into GenX packages, and the AegisDesk repository keeps its prompts, policy rules, domain tools (the access and ticket MCP servers), approval routes, evaluation datasets and UI customisations. The rest is declared:"),
    code(`apiVersion: genx.northstar.example/v1
kind: AgentApplication
metadata:
  name: aegisdesk
  owner: it-service-management
  costCentre: CC-IT-210
  riskTier: medium                        # set at intake in Credo AI
spec:
  cells: [gcp-eu-1, aws-eu-1, azure-eu-1]
  channels: [web, teams, a2a]
  models: { default: primary, router: small-fast }   # aliases, resolved per cloud
  agents:
    - { name: supervisor, template: genx/supervisor@2 }
    - name: access
      template: genx/tool-agent@2
      prompt: aegisdesk/access@prod
      tools: [mcp://access-read/*, mcp://access-action/create_access_request]
  policy:
    rules: policy/aegisdesk.yaml
    autonomy: { default: A3, onElevatedRisk: A2 }
  approvals:
    - workflow: genx/multi-step-approval@1
      route: [manager, security-if-privileged, data-owner-if-confidential]
      ttlHours: 72
  memory: { longTerm: true, retentionDays: 180 }
  evals:
    suites:
      - evals/datasets/golden_v1.yaml
      - evals/adversarial/security_v1.yaml
      - evals/reliability/faults_v1.yaml
    online: [citation-grounding, routing, tool-correctness]
  budgets: { monthly: 4000, tokensPerUserPerDay: 200000 }
  slo: { availability: "99.9", p95Seconds: 15 }`),
    p("The manifest is illustrative. The acceptance test for re-platforming is **parity**: the same evaluation suites, on the same datasets and models, produce results no worse than the committed baselines before the move."),
    h2("22.9 Operating model"),
    table(
      ["Activity", "GenX platform team", "Use-case team", "AI governance office", "Security", "FinOps"],
      [
        ["Platform components and their SLOs", "A, R", "C", "C", "C", "I"],
        ["Agents, prompts, tools of a use case", "C", "A, R", "C", "C", "I"],
        ["Risk tier and required controls", "I", "C", "A, R", "C", "I"],
        ["Red teaming and guardrail policies", "R (tooling)", "C", "C", "A, R", "I"],
        ["Budgets and unit economics", "C", "R", "I", "I", "A"],
        ["Go-live approval", "C", "R", "A", "C", "C"],
        ["Incident response", "R (platform)", "R (use case)", "I", "A (security incidents)", "I"],
      ],
      [0.25, 0.15, 0.14, 0.18, 0.15, 0.13],
    ),
    tableCaption("Responsibilities (A accountable, R responsible, C consulted, I informed)"),
    h2("22.10 How the platform is measured"),
    ...bullets([
      "Time from intake to production for a new use case.",
      "Share of each use case built from platform components rather than its own code.",
      "Evaluation pass rate at the first attempt, and incidents per use case.",
      "Cost per resolved request, per use case.",
      "Share of use cases with complete, current governance evidence.",
    ]),
  );

  // -- 23 roadmap ------------------------------------------------------------------------------
  add(
    h1("23 Roadmap"),
    ...figure("35-roadmap", "Roadmap: sequencing within and across the three phases", { maxH: 640 }),
    table(
      ["Phase", "Scope", "Exit criteria"],
      [
        ["1 Productionise", "Okta OIDC replaces the trusted header; secret managers and workload identity; IdP-issued delegated tokens; one signed image on GKE, EKS and AKS; Terraform cell modules; managed PostgreSQL with HA and point-in-time recovery; WORM audit copies; Temporal for approvals; OTel export to Arize; one primary and one warm standby; first DR game day", "The same digest runs in all three clouds; a DR game day meets the RTO and RPO targets; evaluation gates pass on real models in every cloud; a penetration test has no open high findings; the runbook covers every new component"],
        ["2 Integrate", "Context adapters for Foundry, AgentCore and Gemini; Lyzr registry and deployments; Apigee gateways and API hub; A2A endpoint; Entra Agent ID, Veza, Astrix; FinOps metering; Zscaler red teaming and AI Guard; Rubrik; Credo AI; decision risk index and autonomy levels; the ring of cells", "Each integration’s “validate first” checks pass in a PoC before rollout; the systems-of-record table is enforced; autonomy levels and the kill switch are tested in a game day; cost per resolved request is reported"],
        ["3 GenX", "Extract components into the GenX SDK; templates and portal; manifest format; re-platform AegisDesk; onboard two further use cases", "AegisDesk on GenX shows parity with its baselines; the new use cases reach production through the standard path; platform metrics (§22.10) are agreed and tracked"],
      ],
      [0.17, 0.48, 0.35],
    ),
    tableCaption("Phases, scope and exit criteria"),
    ...callout("note", "The roadmap sets sequence and exit criteria, not dates. Estimates depend on team size and on the PoC results; they should be set after the Phase 1 plan is broken into milestones like M0–M11."),
  );

  // -- 24 decisions ------------------------------------------------------------------------------
  add(
    h1("24 Decisions to record"),
    p("Each of these becomes an architecture decision record when its phase starts, continuing the numbering after ADR 0017."),
    table(
      ["ADR", "Decision", "Main reason"],
      [
        ["0018", "Cells in a ring of warm standbys across the three clouds", "Every cloud carries traffic; a failure affects one cell; standbys are exercised"],
        ["0019", "Build once, sign, promote the same digest to three registries through GitOps", "What was tested is what runs, everywhere"],
        ["0020", "Ports and adapters for context, memory, prompts, workflows and secrets", "Use each cloud’s services without binding the core to them"],
        ["0021", "Git is the source of truth for prompts and policies; cloud stores hold verified copies", "Review and evaluation cannot be bypassed from a console"],
        ["0022", "Temporal owns long-running processes; LangGraph owns the turn", "Timers, retries, compensation and replication for processes that span days"],
        ["0023", "OIDC and token exchange replace the trusted header and the shared HS256 secret", "Verified identity end to end, no shared signing secret"],
        ["0024", "Apigee as MCP registry and gateway; the AegisDesk policy gateway stays the business decision point", "Central control of tool access without moving business rules into a gateway"],
        ["0025", "A2A between agents of different teams; MCP for tools", "Agents keep their own reasoning and approvals"],
        ["0026", "A system of record for every fact (section 21)", "Overlapping products must not contradict each other"],
        ["0027", "Redaction before export; Arize for AI observability; a decision risk index drives autonomy levels", "Production quality evidence and a behavioural circuit breaker"],
        ["0028", "Compensation first, Rubrik rollback second; undo goes through governance", "Precise undo for single actions, a safety net for broad damage"],
        ["0029", "Red-team regression is a release gate", "A release may not be easier to attack than the previous one"],
        ["0030", "Credo AI holds policy intent and attestations; enforcement stays in code", "Governance at enterprise scale without an unevaluated enforcement point"],
        ["0031", "A canonical memory record, with native memory services as engines and caches", "Memories survive failover, and deletion is provable"],
        ["0032", "Static stability: no synchronous control-plane call on the request path", "Control-plane outages stop releases, not users"],
      ],
      [0.08, 0.5, 0.42],
    ),
    tableCaption("Decision candidates"),
  );

  // -- 25 risks ------------------------------------------------------------------------------------
  add(
    h1("25 Risks"),
    table(
      ["Risk", "Impact", "Mitigation"],
      [
        ["Integration sprawl: more than fifteen products with overlapping features", "Complexity, cost, unclear ownership", "Systems-of-record table; phase gates; PoC checks before each integration; ports keep products replaceable"],
        ["Vendor capabilities differ from announcements, or stay in preview", "Rework, delays", "“Validate first” checks; no preview feature on the request path without a fallback"],
        ["More dependencies on the request path", "Lower availability, higher latency", "Criticality table (§20.2); timeouts and breakers; static stability"],
        ["Behaviour differs between clouds (model versions, safety settings)", "Inconsistent answers; surprises at failover", "One model family everywhere; evaluation baselines per cloud; pinned versions"],
        ["Data transfer charges between clouds", "Cost", "Traffic stays in the home cell; replicate only what DR needs"],
        ["Memory poisoning and privacy failures", "Manipulated behaviour, data exposure", "Memory rules (§8.6); red-team memory scenarios; deletion propagation"],
        ["Identity complexity with Okta and Entra", "Outages, privilege gaps", "One IdP of record, federation, Veza verification, access reviews"],
        ["Lock-in to control-plane products", "Switching cost", "Git as the source for policies and prompts; exportable registry data"],
        ["Regulatory change", "Compliance gaps", "Policy packs maintained centrally in Credo AI; evidence produced automatically"],
        ["Skills and operating model", "Slow delivery", "Platform team, templates, this tutorial, and milestone-by-milestone delivery as in M0–M11"],
      ],
      [0.3, 0.22, 0.48],
    ),
    tableCaption("Risks and mitigations"),
  );

  // -- 26 glossary ------------------------------------------------------------------------------
  add(
    h1("26 Glossary"),
    table(
      ["Term", "Meaning"],
      [
        ["A2A", "Agent2Agent protocol: a standard for agents to discover each other (Agent Cards) and delegate tasks"],
        ["Activity (Temporal)", "A workflow step with side effects, retried according to a policy"],
        ["Autonomy level", "How much the agents may do without a person; lowered automatically when decision risk rises"],
        ["Canary", "A new release serving a small share of traffic while it is compared with the stable one"],
        ["Cell", "A complete copy of the stack in one cloud region, serving a subset of users"],
        ["Compensation", "A domain action that reverses an earlier one (the “undo button”)"],
        ["Control plane / data plane", "Where the system is configured / where requests are served"],
        ["FOCUS", "FinOps Open Cost and Usage Specification: a common schema for cost data"],
        ["GitOps", "Deployment by declaring the desired state in git; each cluster pulls and applies it"],
        ["MCP", "Model Context Protocol: the protocol between agents and tool servers"],
        ["NHI", "Non-human identity: service accounts, API keys, tokens, agent identities"],
        ["OpenInference", "Semantic conventions on top of OpenTelemetry for model, retrieval and tool spans"],
        ["Port / adapter", "An interface the core depends on / a provider-specific implementation of it"],
        ["RPO / RTO", "How much recent data may be lost / how long the service may be down"],
        ["Saga", "A sequence of steps with compensations, so a failure part-way can be undone"],
        ["SBOM", "Software bill of materials: the list of components inside an artifact"],
        ["Signal (Temporal)", "An external event delivered to a running workflow"],
        ["Static stability", "The request path keeps working on last-known configuration when the control plane is down"],
        ["Token exchange", "OAuth (RFC 8693): trading a user’s token for a narrower one that also names the acting agent"],
        ["Warm standby", "A scaled-down copy with replicated data, ready to take over"],
        ["WORM", "Write once, read many: storage that cannot be modified or deleted during retention"],
        ["XAA", "Cross App Access: an OAuth extension for agents to access enterprise applications under IdP policy"],
      ],
      [0.24, 0.76],
    ),
    tableCaption("Glossary"),
  );

  // -- 27 sources --------------------------------------------------------------------------------
  add(
    h1("27 Sources"),
    p("Vendor facts were taken from these public sources in September 2026. They describe products that change quickly; confirm them with each vendor before relying on them."),
    table(
      ["Topic", "Source"],
      [
        ["Apigee API hub: MCP support, release notes", "docs.cloud.google.com/apigee/docs/apihub/release-notes"],
        ["Apigee API hub and Agent Registry integration", "docs.cloud.google.com/apigee/docs/apihub/manage-agent-registry-integration"],
        ["Gemini Enterprise Agent Platform", "cloud.google.com/blog/products/ai-machine-learning/introducing-gemini-enterprise-agent-platform"],
        ["Memory Bank", "docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank/api-quickstart"],
        ["Amazon Bedrock AgentCore", "docs.aws.amazon.com/bedrock-agentcore/latest/devguide/what-is-bedrock-agentcore.html; aws.amazon.com/about-aws/whats-new/2025/10/amazon-bedrock-agentcore-available"],
        ["Microsoft Foundry memory", "learn.microsoft.com/en-us/azure/foundry/agents/concepts/what-is-memory"],
        ["Temporal Cloud High Availability", "docs.temporal.io/cloud/high-availability; temporal.io/blog/expanding-temporal-clouds-high-availability-offerings"],
        ["A2A protocol", "a2a-protocol.org/latest/specification; opensource.googleblog.com/2026/04/a-year-of-open-collaboration-celebrating-the-anniversary-of-a2a.html"],
        ["Lyzr Agent Control Plane", "lyzr.ai/blog/lyzr-agent-control-plane"],
        ["Okta for AI Agents", "support.okta.com (What’s New in Okta for AI Agents); okta.com newsroom"],
        ["Microsoft Entra Agent ID, identity comparison", "beri.net/article/okta-vs-entra-agent-id-vs-sailpoint-agent-identity-2026"],
        ["Veza", "servicenow.com/products/veza.html; veza.com/resources/veza-access-agent-deep-dive"],
        ["Astrix Security", "astrix.security/product; astrix.security/security-programs/agentic-ai-security"],
        ["Credo AI", "credo.ai/product"],
        ["Arize AX and Phoenix", "arize.com; arize.com/docs/ax/release-notes"],
        ["Flexera AI cost management", "flexera.com/products/ai-cost-management; flexera.com/blog/finops/finopsx-2026-product-announcements"],
        ["IBM Apptio Cloudability", "apptio.com/products/cloudability; apptio.com/innovation-hub"],
        ["OptScale (possible match for “Optiz”)", "github.com/hystax/optscale"],
        ["Rubrik Agent Rewind", "rubrik.com/insights/ai-issues-take-control-with-rubrik-agent-rewind; techrepublic.com/article/news-fix-ai-agent-mistakes-rubrik-agent-rewind"],
        ["Zscaler and SPLX AI red teaming", "securityweek.com/zscaler-acquires-ai-security-company-splx; splx.ai/resources/automated-ai-red-teaming"],
        ["AegisDesk as built", "`docs/AegisDesk-High-Level-Design.docx`, `docs/ARCHITECTURE.md`, ADRs 0001–0017, milestone notes M0–M11"],
      ],
      [0.34, 0.66],
    ),
    tableCaption("Sources"),
  );
}
