// Sections 8–13: agent platforms, Lyzr, Apigee, Temporal, A2A, identity and entitlements.
export function integrationsA(k) {
  const { p, h1, h2, bullets, numbered, table, tableCaption, callout, code, figure, add } = k;

  // -- 8 agent platforms ---------------------------------------------------------------------
  add(
    h1("8 Agent platforms: Microsoft Foundry, Amazon Bedrock AgentCore, Gemini Enterprise"),
    p("**Integration 1: conversation and context management, prompt management, memory management.**"),
    h2("8.1 In plain words"),
    ...callout("plain", [
      "A good human assistant remembers what you said a minute ago (the **session**), knows useful facts about you from earlier conversations (**long-term memory**), follows a well-rehearsed and reviewed script (the **prompt**), and decides what to keep in mind for the task at hand (**context management**).",
      "Each cloud now sells these as managed services next to its models. This section connects AegisDesk to all three without making the agents depend on any one of them.",
    ]),
    h2("8.2 What each platform offers"),
    table(
      ["Capability", "Microsoft Foundry", "Amazon Bedrock AgentCore", "Gemini Enterprise Agent Platform"],
      [
        ["Managed runtime", "Foundry Agent Service", "AgentCore Runtime: serverless, isolated sessions, framework-agnostic, supports MCP and A2A", "Agent Engine"],
        ["Conversation", "Conversations (threads), with automatic truncation to fit the model’s context", "Memory short-term: the raw events of a session", "Sessions"],
        ["Long-term memory", "Memory (user, session and procedural types; parts in preview) with an extract, consolidate, retrieve lifecycle; retrieved memories are added to the prompt", "Memory long-term: strategies that extract facts, preferences and summaries from events", "Memory Bank: generates and consolidates memories from sessions, scoped to a user"],
        ["Prompts", "Agent definitions with instructions and versions", "Bedrock Prompt Management (versioned prompts)", "Vertex AI prompt management"],
        ["Tools", "Tools, including MCP", "Gateway turns APIs and Lambda functions into MCP tools; Policy in Cedar attached to the Gateway (log-only or enforce, default deny)", "Agent Development Kit tools, MCP"],
        ["Identity", "Microsoft Entra Agent ID", "AgentCore Identity: agent identities, propagation of the caller’s identity", "Google Cloud IAM"],
        ["Observability and evaluation", "Tracing and evaluations", "AgentCore Observability (OpenTelemetry), Evaluations", "Tracing and evaluation"],
        ["Employee channel", "Microsoft 365 Copilot and Teams", "—", "Gemini Enterprise app"],
      ],
      [0.16, 0.28, 0.28, 0.28],
    ),
    tableCaption("Context, memory and prompt capabilities of the three agent platforms (public documentation, September 2026)"),
    h2("8.3 Why AegisDesk needs it"),
    ...callout("why", [
      "• **No long-term memory.** Every conversation starts from zero. An employee who reported a VPN problem yesterday has to explain it again today.",
      "• **Context is assembled ad hoc** inside each agent. There is no single place that enforces a token budget, records what went into the prompt, or keeps retrieved text labelled as data.",
      "• **Prompts ship inside the image** (`src/aegisdesk/prompts/`, loaded by `prompts/loader.py`). That is safe, but there is no labelled promotion, no per-cloud experiment, and the cloud platforms’ own tools cannot see which prompt version produced an answer.",
      "• Building memory extraction, consolidation, conflict resolution and scalable retrieval ourselves would repeat what these services already provide.",
    ]),
    h2("8.4 How it plugs in: the context service port"),
    ...figure("11-agent-platform-abstraction", "One context service port; an adapter per platform, plus a local adapter for tests and fallback", { maxH: 620 }),
    ...callout("plug", [
      "A new **context service** sits between the agents and the platforms, with three groups of operations: **sessions** (append conversation events, read recent turns), **memory** (recall, record, forget) and **prompts** (resolve a name and label to a verified version). Each cell is configured with the adapter of its cloud. The LangGraph checkpointer stays the source of truth for graph state, because interrupts, approvals and replay depend on it; the provider session receives a **mirror** of the user and assistant messages so the provider’s memory extraction can work.",
    ]),
    code(`class MemoryPort(Protocol):
    def recall(self, user: UserContext, query: str, k: int = 5) -> list[Memory]: ...
    def record(self, user: UserContext, events: Sequence[ConversationEvent]) -> None: ...
    def forget(self, user: UserContext, memory_id: str | None = None) -> None: ...

class PromptPort(Protocol):
    def resolve(self, name: str, label: str = "prod") -> PromptVersion: ...  # verified`),
    h2("8.5 Conversation and context management"),
    ...figure("12-context-assembly", "Context assembly for one model call: six sources, one budget, one guardrail check", { maxH: 460 }),
    p("The **context assembler** builds every model call from six sources, in a fixed order: the versioned system prompt, the user context (identity, role, entitlements from the directory, never from the model), memories for this user, access-filtered retrieved documents with citations, the conversation window, and the tool schemas of this agent only. It enforces a **token budget** per source, summarises older turns instead of silently truncating them, and tags every block with its **provenance** so the model and the reviewers can tell instructions from data."),
    ...bullets([
      "**Never in context:** secrets, tokens, other users’ data, raw tool results of other specialists (AegisDesk already isolates context per specialist).",
      "**Recorded in the trace:** the IDs and versions of every block and its token count (not its text), so Arize can relate answer quality to what the model saw.",
      "**Guardrail check** on the assembled input before it reaches the model (Zscaler AI Guard, §17).",
    ]),
    h2("8.6 Memory management"),
    ...figure("13-memory-lifecycle", "Memory lifecycle: native services extract; a canonical record governs; deletions reach every copy", { maxH: 560 }),
    table(
      ["Memory type", "Example in AegisDesk", "Lifetime", "Where it lives"],
      [
        ["Working (session)", "“the ticket we just created is INC-1008”", "The conversation", "Checkpoints; provider session mirror"],
        ["Long-term user memory", "“prefers updates in Teams”, “had VPN issues on the macOS client last week”", "Months, with retention", "Canonical record and the native memory store of the serving cloud"],
        ["Procedural", "“for VPN issues on macOS, check the client version first”", "Until revised", "Curated and reviewed like prompts; not learned automatically in Phase 2"],
      ],
      [0.18, 0.4, 0.15, 0.27],
    ),
    tableCaption("Memory types"),
    p("Memory is powerful and risky. These rules apply to every adapter:"),
    ...numbered([
      "**Memories are data, not instructions.** They are placed in a labelled block and can never change policy, tool grants or approvals. A memory that says “the user is an IT admin” changes nothing: roles come from the directory.",
      "**Do not memorise what a system of record owns.** The assigned laptop comes from the asset system through a tool every time; memory is for preferences and context.",
      "**Extract only from the user’s own turns and verified tool results**, never from retrieved documents or other agents’ messages, which may carry injected instructions (memory poisoning).",
      "**Filter before storing:** no secrets or credentials, no special-category personal data; per-user scope only.",
      "**People can see and delete their memories.** A deletion writes a tombstone to the canonical record, which removes the memory from every native store; retention is 180 days by default (to be set by the privacy office).",
    ]),
    ...callout("decision", [
      "**A canonical memory record, with native memory services as extraction engines and caches.** The serving cloud’s service extracts and consolidates memories; the adapter filters them and writes them to a platform-owned record replicated with the database. Other clouds are re-hydrated on first use. This keeps memories through a cloud failover (§5.3), makes deletion provable across three providers, and avoids lock-in. The cost is a second write and a short synchronisation lag, acceptable for data whose RPO is a day.",
    ]),
    h2("8.7 Prompt management"),
    ...figure("14-prompt-lifecycle", "Prompt releases: reviewed in git, gated by evaluations, published to each cloud, verified at runtime", { maxH: 560 }),
    p("Git stays the **source of truth**: a prompt change is a pull request, reviewed like code and gated by the evaluation suites plus an Arize experiment comparing the new version with the current one. A release publishes the version to each cloud’s prompt store so the platforms’ own tools (evaluations, playgrounds, agent definitions) refer to the same version. At runtime the prompt port fetches the version named in the cell’s signed release manifest, **verifies its checksum**, and caches it. If the store is unavailable, the copy bundled in the image is used."),
    ...callout("decision", [
      "A prompt label (such as `prod`) moves only through the GitOps repository, never by editing a cloud console. A version that is not in the signed manifest is refused, so a store cannot be used to bypass review.",
    ]),
    h2("8.8 Validate first"),
    ...callout("caution", [
      "• Quality of memory extraction on AegisDesk-style conversations: precision of extracted facts, and no personal data outside the filter rules.",
      "• Retrieval latency within budget (target p95 under 300 ms) and behaviour under provider throttling.",
      "• Deletion semantics and timing in each memory service, data residency of stored memories, and whether memories can be exported for the canonical record.",
      "• Preview status and pricing of each memory feature (some are billed per memory event).",
      "• Mirroring sessions does not leak tool results containing sensitive data (mirror user and assistant messages only).",
    ]),
  );

  // -- 9 Lyzr ------------------------------------------------------------------------------------
  add(
    h1("9 Lyzr: agent registry and AI deployment management"),
    p("**Integrations 2 and 10: agent registry; AI deployment management.**"),
    h2("9.1 In plain words"),
    ...callout("plain", "An HR system for agents. Every agent gets a record: who owns it, what it is for, which models, tools and data it uses, which version runs where, and whether it passed its checks. The deployment side is the agents’ release office: it knows which version was promoted where, by whom, and on what evidence."),
    h2("9.2 What Lyzr provides"),
    p("Lyzr’s Agent Control Plane describes an **agent registry** that records each agent’s identity, ownership, purpose, models, tools, MCP servers, permissions, environment, evaluation status and deployment history. It is framework-agnostic (LangGraph, CrewAI, Strands) and targets several runtimes (for example AgentCore and Agent Engine). Its deployment pipeline covers repository push, security scan, container build and scan, deployment, health check, identity registration (a per-agent Okta identity) and automated evaluations."),
    h2("9.3 Why AegisDesk needs it"),
    ...callout("why", [
      "• Agents exist only as code (`agents/`, `graphs/`) and grants in `config/policy.yaml`. Nobody outside the repository can see them.",
      "• An enterprise with many agents must answer, at any time: which agents exist, who owns each one, what it can touch, which version was running at the time of an incident, and whether that version passed its evaluations. Auditors and the AI governance office will ask exactly these questions.",
      "• Unregistered (“shadow”) agents need a place to be registered once they are discovered (Astrix, §13).",
    ]),
    h2("9.4 How it plugs in"),
    ...figure("15-registries", "Lyzr as the agent system of record, linked to identity, tools, risk, evaluations, access and discovery", { maxH: 420 }),
    table(
      ["Step", "Performed by", "Recorded in Lyzr"],
      [
        ["Build, test, evaluate, sign", "CI pipeline (§6)", "New version with evaluation status, SBOM and provenance links"],
        ["Promotion request (for example staging to production AWS cells)", "Engineer, through Lyzr or a GitOps pull request", "Request and approver"],
        ["Gate check", "Lyzr reads the evaluation status and the Credo AI governance status", "Gate result"],
        ["Rollout", "Argo CD for Kubernetes; the runtime’s deployment API for managed runtimes", "Deployment record per cell and cloud"],
        ["Identity", "Agent identity created or linked in Okta or Entra", "Identity reference and human owner"],
        ["Retirement", "Lyzr decommission workflow", "Identity revoked, grants removed, record archived"],
      ],
      [0.34, 0.33, 0.33],
    ),
    tableCaption("Division of labour between the pipeline, the deployers and Lyzr"),
    p("For AegisDesk the registry holds the router, the supervisor, the Knowledge, Service Desk and Access specialists and the approval workflow identity, each with its tools referenced from the Apigee API hub (not copied), its policy grants shown from the policy bundle, and its evaluation results from CI and Arize."),
    ...callout("decision", [
      "**Lyzr is the system of record for the agent inventory and deployment history; the policy file remains the enforcement authority for what an agent may do.** Lyzr displays the grants from the released policy bundle but does not enforce them, so a registry edit can never widen an agent’s permissions. The runtime never calls Lyzr on the request path (static stability).",
    ]),
    h2("9.5 Validate first"),
    ...callout("caution", [
      "• API for registering versions and deployments from CI, and webhooks for promotion events.",
      "• Whether deployment management drives our GitOps flow (or deploys directly) for LangGraph workloads on Kubernetes, not only on managed runtimes.",
      "• The Okta identity registration flow, and linking to Entra Agent ID for Azure-hosted agents.",
      "• Export of registry data, and synchronisation with Credo AI’s agent registry and the Apigee API hub without duplicate ownership.",
    ]),
  );

  // -- 10 Apigee ---------------------------------------------------------------------------------
  add(
    h1("10 Apigee: MCP registry, MCP gateway and agent gateway"),
    p("**Integration 3.**"),
    h2("10.1 In plain words"),
    ...callout("plain", [
      "Picture an airport. The **API hub** is the departures board: it lists every flight (tool server), its gate, its airline (owner) and its rules. The **MCP gateway** is the security checkpoint every passenger passes on the way to a gate: it checks the boarding pass (token) and counts passengers (quotas). The **agent gateway** is the arrivals hall, where other agents coming to AegisDesk are checked before they get in.",
    ]),
    h2("10.2 What Apigee provides"),
    ...bullets([
      "**API hub** catalogues APIs and treats **MCP as an API style**, with the servers and their tools as first-class entries. It can generate a managed MCP server from an OpenAPI specification and deploy it to an Apigee runtime, and it synchronises MCP server and tool metadata with Google’s Agent Registry. API hub also offers its own MCP server (generally available since July 2026) so agents can discover APIs.",
      "**Apigee proxies** provide token verification, quotas and spike arrest, threat protection, routing and analytics in front of any backend, including MCP servers and agents.",
      "**Apigee hybrid** runs the runtime plane on Kubernetes in other clouds (EKS, AKS) while the management plane stays in Google Cloud.",
    ]),
    h2("10.3 Why AegisDesk needs it"),
    ...callout("why", [
      "Today AegisDesk has two internal MCP servers, one client (the API), and a shared signing secret. In production there will be many MCP servers owned by different teams (ITSM, IGA, HR) and many agents calling them. Without a registry nobody knows which tools exist, who owns them or which version is current. Without a gateway every server re-implements authentication, quotas and logging, and point-to-point connections cannot be governed or switched off centrally. And generating MCP servers from existing OpenAPI specifications is the fastest safe way to reach real enterprise systems.",
    ]),
    h2("10.4 How it plugs in"),
    ...figure("16-apigee-gateways", "Agent gateway for inbound calls, MCP gateway for tool calls, API hub as the tool registry", { maxH: 600 }),
    ...numbered([
      "**Inbound:** channels and other agents reach AegisDesk only through the agent gateway, which verifies the Okta or Entra token, applies quotas, and routes REST and A2A traffic to the user’s home cell.",
      "**Tools:** `tools/remote.py` calls MCP servers through the MCP gateway. The gateway verifies the delegated token (`aud` is this server, `act` is a registered agent), checks that the tool is in the calling agent’s API product, applies quotas and logs the call. The MCP server still verifies the token itself, as today.",
      "**Discovery:** the pipeline publishes AegisDesk’s MCP servers to API hub. The tool catalogue each cell uses is built from API hub at release time and shipped in the configuration bundle, not looked up per request.",
    ]),
    table(
      ["", "Apigee (agent and MCP gateways)", "AegisDesk policy gateway (`governance/gateway.py`)"],
      [
        ["Question it answers", "May this caller reach this server or tool at all, at this rate?", "May this agent, for this user, take this action with these arguments now?"],
        ["What it knows", "Tokens, client applications, API products, quotas", "User roles, ownership, risk level, approval evidence, write budget"],
        ["Typical denial", "Invalid token, quota exceeded, tool not in the product", "Missing approval, write budget exceeded, not the owner"],
        ["Changed by", "Platform team", "Use-case team, reviewed and evaluated like code"],
      ],
      [0.2, 0.4, 0.4],
    ),
    tableCaption("Two gateways, two different questions"),
    ...callout("decision", [
      "**Both gateways stay, and neither trusts the other.** Apigee is the coarse, network-level policy enforcement point; the AegisDesk policy gateway remains the business-level one. The delegated token travels end to end, so an MCP server verifies it even when a call reached it without passing the gateway.",
    ]),
    h2("10.5 Validate first"),
    ...callout("caution", [
      "• MCP protocol versions and streaming (Streamable HTTP) through Apigee proxies; added latency per tool call.",
      "• Operating the Apigee hybrid runtime on EKS and AKS, and its behaviour when the management plane is unreachable.",
      "• Quality of generated MCP servers for our ITSM and IGA OpenAPI specifications (tool names, descriptions, argument schemas).",
      "• Token verification policies for delegated tokens with `act` claims; synchronisation between API hub, Agent Registry and Lyzr.",
    ]),
  );

  // -- 11 Temporal -------------------------------------------------------------------------------
  add(
    h1("11 Temporal: agent orchestration and planning"),
    p("**Integration 4.**"),
    h2("11.1 In plain words"),
    ...callout("plain", "A durable workflow is a to-do list that cannot forget. If the computer running it dies halfway through, another one continues at exactly the same step with the same variables. Waiting three days for a manager costs nothing: the workflow sleeps until a signal (the decision) or a timer (the deadline) wakes it."),
    h2("11.2 Concepts"),
    table(
      ["Concept", "Meaning", "In AegisDesk"],
      [
        ["Workflow", "Deterministic code describing a process; its history is recorded and replayed after a crash", "The access approval process"],
        ["Activity", "A step with side effects, retried according to a policy", "Provision access, verify access, send an approval card"],
        ["Signal", "An external event delivered to a running workflow", "Approve or reject from the API or a Teams card"],
        ["Query", "Read the state of a running workflow", "Progress shown in the UI"],
        ["Timer", "A durable sleep", "Approval expiry (TTL)"],
        ["Worker and task queue", "Processes that poll for work; they can run in any cloud", "Workers in every cell"],
        ["Saga (compensation)", "Undo completed steps when a later step fails", "Revoke a partial grant"],
        ["Schedule", "Start a workflow on a calendar", "Nightly checks that replace manual reconciliation"],
        ["Namespace", "Unit of isolation, replication and access control", "One per use case and environment"],
      ],
      [0.2, 0.45, 0.35],
    ),
    tableCaption("Temporal concepts"),
    h2("11.3 Why AegisDesk needs it"),
    ...callout("why", [
      "Approvals already work with LangGraph interrupt and resume on PostgreSQL checkpoints (ADR 0011), and that remains correct. But the approval process spans days, involves several people and systems, and needs timers; the TTL is checked when someone looks, not scheduled. Provisioning after approval can fail after the point of no return, which is why M11 added `approvals reconcile` as a command an operator must run. And a paused approval survives a cloud failure only as well as the database replica does. Temporal provides timers, retries, compensation, visibility and cross-cloud replication as a service.",
    ]),
    h2("11.4 How it plugs in"),
    table(
      ["Concern", "LangGraph (the turn)", "Temporal (the process)"],
      [
        ["Scope", "One conversational turn: route, reason, propose tool calls", "A business process spanning hours or days"],
        ["Duration", "Seconds", "Minutes to weeks"],
        ["Probabilistic decisions", "Yes: the model proposes", "None: it executes a deterministic process"],
        ["State", "Graph state in checkpoints", "Workflow history"],
        ["Failure handling", "M11 guards, safe answers", "Retries, timeouts, compensation"],
      ],
      [0.22, 0.39, 0.39],
    ),
    tableCaption("Division of labour between LangGraph and Temporal"),
    ...figure("17-temporal-approval", "The access approval workflow: signals, timers, activities through the policy gateway, verification and compensation", { maxH: 560 }),
    p("When the policy engine requires approval, the runtime starts `AccessApprovalWorkflow` with the **access request ID as the workflow ID**, so a retried start cannot create a second workflow. Approval decisions from the API or a Teams card are written to the audit log first (write-ahead, as in M11) and then delivered as **signals**. Provisioning is an **activity** that calls the policy gateway under the workflow’s own identity, which remains the only identity allowed to provision. After provisioning, a second activity asks Veza to verify the effective access (§13). Finally the workflow resumes the conversation thread so the employee sees the outcome."),
    code(`@workflow.defn
class AccessApprovalWorkflow:
    def __init__(self) -> None:
        self.decisions: dict[str, Decision] = {}

    @workflow.signal
    def decide(self, step: str, decision: Decision) -> None:  # audited before sending
        self.decisions[step] = decision

    @workflow.run
    async def run(self, request: AccessRequest) -> Outcome:
        for step in request.approval_steps:        # manager, security, data owner
            try:
                await workflow.wait_condition(
                    lambda: step in self.decisions, timeout=APPROVAL_TTL)
            except asyncio.TimeoutError:
                return await self.close(request, Outcome.expired(step))
            if self.decisions[step].rejected:
                return await self.close(request, Outcome.rejected(step))
        await workflow.execute_activity(
            provision_access, request, retry_policy=PROVISION_RETRY,
            start_to_close_timeout=timedelta(minutes=2))
        await workflow.execute_activity(
            verify_effective_access, request,
            start_to_close_timeout=timedelta(minutes=5))
        return await self.close(request, Outcome.granted())`),
    p("The sketch leaves out compensation and notifications. It is illustrative, not a finished implementation."),
    h2("11.5 Planning: the LLM plans, code checks, Temporal executes"),
    ...figure("18-plan-execute", "Plan and execute: a structured plan from the model, deterministic validation, durable execution", { maxH: 600 }),
    p("Some requests need several coordinated actions, for example setting up a new joiner: a laptop ticket, VPN, three application accesses and a welcome message. The model produces a **structured plan** (steps with allowlisted tools and arguments). Code validates it: schema checks, a **policy dry run** of every step (the policy engine evaluates each step without executing it, a new capability of the engine), the total risk and the write budget. A plan containing a high-risk step is shown to a human in plain language before anything runs. Temporal then executes one activity per step through the policy gateway, exposes progress through queries, and compensates completed steps in reverse order if a later one fails. If a step needs input or fails, the planner may be asked to re-plan the remaining steps, and the new plan goes through the same validation."),
    ...callout("decision", "The model never drives a long-running process directly. It can propose a plan, and a deterministic workflow executes it under the same policy, approval and audit rules as a single tool call."),
    h2("11.6 Disaster recovery and data protection"),
    p("Temporal Cloud’s High Availability replicates a namespace to another region or to another cloud (AWS and Google Cloud), with automatic failover, a published RPO under one minute, a published RTO of 20 minutes and a 99.99% availability SLA. Temporal Cloud is hosted in AWS and Google Cloud regions. Azure-hosted workers connect to it over TLS; the extra latency affects workflow control traffic only, not the chat turn. If a regulator requires Azure-only hosting for a use case, Temporal can be self-hosted on AKS, and then we operate it ourselves."),
    ...callout("decision", "Workflow payloads are **encrypted in the worker** with a payload codec before they reach Temporal Cloud, so employee data in approval requests never reaches the service in clear text. The keys stay in our key management."),
    h2("11.7 Validate first"),
    ...callout("caution", [
      "• A failover drill of a High Availability namespace between AWS and Google Cloud, with in-flight approvals.",
      "• Latency and reliability of Azure-hosted workers against Temporal Cloud.",
      "• The payload codec with our key management; versioning of workflow code while approvals are in flight.",
      "• Cost per workflow action at our expected volume.",
    ]),
  );

  // -- 12 A2A --------------------------------------------------------------------------------
  add(
    h1("12 A2A protocol: multi-agent communication"),
    p("**Integration 5.**"),
    h2("12.1 In plain words"),
    ...callout("plain", [
      "**MCP is using a tool:** you give a calculator exact inputs and get an output. **A2A is asking a colleague in another department:** you describe what you need, they may ask a follow-up question, take a while, and hand back results. The colleague has their own judgement and tools you never see.",
    ]),
    h2("12.2 Concepts"),
    table(
      ["Concept", "Meaning"],
      [
        ["Agent Card", "A JSON document an agent publishes at `/.well-known/agent-card.json`: name, description, skills, endpoint and protocol binding, accepted authentication. It can be signed, so a caller can verify integrity and domain ownership"],
        ["Task", "A unit of work with a lifecycle, such as submitted, working, input-required, completed, failed or canceled"],
        ["Message and parts", "What the agents exchange: text, files, structured data"],
        ["Artifact", "A result produced by a task, such as a ticket reference"],
        ["Streaming and push notifications", "Progress over server-sent events, or a webhook for long tasks"],
        ["Bindings", "Version 1.0 (March 2026) defines JSON-RPC 2.0 over HTTPS, gRPC, and HTTP+JSON (REST)"],
      ],
      [0.25, 0.75],
    ),
    tableCaption("A2A concepts"),
    h2("12.3 Why AegisDesk needs it"),
    ...callout("why", [
      "Other enterprise agents (an HR onboarding agent in Copilot Studio, a finance agent in Gemini Enterprise, agents built on Lyzr) need to ask AegisDesk for tickets and access, and AegisDesk sometimes needs to ask them, for example to confirm a new joiner’s manager. Without a standard, every pair of agents needs a bespoke API.",
      "Exposing AegisDesk as MCP tools instead would bypass its reasoning, approvals and follow-up questions, and turn a governed service into a thin tool. A2A’s tasks fit what actually happens: long-running, interactive work that may need more input.",
    ]),
    h2("12.4 How it plugs in"),
    ...figure("19-a2a-sequence", "An HR agent discovers AegisDesk, exchanges a token that carries both identities, and delegates a task that needs more input", { maxH: 520 }),
    p("The **A2A server** is a new thin adapter over `AegisRuntime`, exactly as the FastAPI API is (ADR 0015): an A2A task maps to an AegisDesk thread, a message to `send_message`, a clarifying question from AegisDesk to the input-required state, and created tickets and access requests to artifacts. The task completes when AegisDesk has done its part; a pending approval is reported in the artifact, and its outcome is sent later as a notification."),
    code(`{
  "name": "AegisDesk IT service desk",
  "description": "Tickets, access requests and IT answers for Northstar employees",
  "version": "1.8.0",
  "capabilities": { "streaming": true, "pushNotifications": true },
  "skills": [
    { "id": "it-help",        "name": "Answer IT questions from company documents" },
    { "id": "create-ticket",  "name": "Create a support ticket for the user" },
    { "id": "request-access", "name": "Request application access (may need approval)" },
    { "id": "request-status", "name": "Status of the user's tickets and requests" }
  ]
}`),
    p("The card above is abbreviated and illustrative; the endpoint, binding and security scheme fields follow the v1.0 specification. Approving or rejecting is deliberately **not** a skill: only people approve (separation of duties)."),
    ...bullets([
      "**Identity:** the caller presents a token for the end user with its own identity in the `act` claim, obtained by token exchange (§13). These are the same claims AegisDesk’s delegation tokens carry today, so the policy engine treats the call exactly like the user’s own request: a calling agent can never do more than the user could.",
      "**Allowlist:** which agents may call which skills is configured per skill and reviewed with the AI governance office.",
      "**Untrusted input:** messages from other agents are untrusted, like user input. The same prompt-injection defences apply, and a message can never raise privileges.",
    ]),
    h2("12.5 The products around an A2A call"),
    ...figure("20-a2a-stack", "Where AgentCore, Zscaler, Apigee, Arize, Rubrik and Credo AI touch an agent-to-agent call", { maxH: 560 }),
    table(
      ["Product", "Role in A2A traffic"],
      [
        ["Amazon Bedrock AgentCore", "AgentCore Runtime supports A2A, so an AWS cell can host AegisDesk’s A2A server on the managed runtime as an alternative to EKS"],
        ["Zscaler", "Inspects messages and payloads for prompt injection and data loss before they reach the agent"],
        ["Apigee", "Authenticates the calling agent, applies quotas, routes to the home cell"],
        ["Arize", "Traces the whole multi-agent task as one trace through W3C trace context propagation"],
        ["Rubrik", "Records the actions taken for the calling agent, so they can be rolled back"],
        ["Credo AI", "Holds the policy intent: which agents may delegate which skills, under which controls"],
      ],
      [0.28, 0.72],
    ),
    tableCaption("Products around agent-to-agent calls"),
    h2("12.6 Validate first"),
    ...callout("caution", [
      "• Maturity of the v1.0 Python SDK and signed Agent Card verification.",
      "• A2A streaming and push notifications through Apigee.",
      "• Interoperability tests with Copilot Studio and Gemini Enterprise agents.",
      "• Token exchange that preserves the `act` claim across Okta and Entra.",
    ]),
  );

  // -- 13 identity ---------------------------------------------------------------------------
  add(
    h1("13 Identity and entitlements: Okta, Microsoft Entra, Veza, Astrix"),
    p("**Integration 6.**"),
    h2("13.1 In plain words"),
    ...callout("plain", [
      "Four jobs. The **badge office** issues badges to people (Okta, Entra ID). **Robots get badges too**, which also say which person they are working for (agent identities, delegated tokens). A **building map** shows which badge actually opens which doors, including doors nobody remembers granting (Veza). And a **patrol** looks for robots without badges and keys left under doormats (Astrix).",
    ]),
    h2("13.2 Roles of the products"),
    table(
      ["Product", "Role in this design"],
      [
        ["Okta", "Workforce identity provider of record: single sign-on (OIDC) for the UI, Teams and the API; authorization server for delegated tokens (token exchange). Okta for AI Agents registers agents as identities (Agent SSO, generally available since August 2026). Cross App Access (XAA) lets agents reach enterprise applications under IdP policy, and has been adopted as MCP’s Enterprise-Managed Authorization extension"],
        ["Microsoft Entra ID and Entra Agent ID", "Identity for the Microsoft estate (AKS workload identity, Foundry, Teams), federated with Okta. Entra Agent ID gives agents running in Azure their own identities (agent identity blueprints, agent identities, agent users)"],
        ["Veza (from ServiceNow)", "Access graph: the effective permissions of people, non-human identities and agents across clouds, SaaS and databases; access reviews; least-privilege analysis; verification after provisioning"],
        ["Astrix", "Discovers AI agents, MCP servers and non-human identities, sanctioned and shadow; flags excessive privileges, secret hygiene problems and anomalous use; just-in-time access"],
      ],
      [0.25, 0.75],
    ),
    tableCaption("Identity and entitlement products"),
    h2("13.3 Why AegisDesk needs it"),
    ...callout("why", [
      "• The API trusts `X-Employee-Id` from a gateway (ADR 0015), which is only safe when the network guarantees that gateway. Production needs verified tokens.",
      "• Delegation tokens use HS256 with a shared secret; `identity/tokens.py` itself notes that a real deployment would use an OAuth authorization server and asymmetric keys.",
      "• AegisDesk’s agents have no enterprise identity lifecycle: no owner in the directory, no credential rotation, no access review.",
      "• Nobody can answer “what can the AegisDesk provisioning identity actually do in FinanceERP?” without reading several systems by hand.",
    ]),
    h2("13.4 How it plugs in"),
    ...figure("21-identity-flow", "User sign-on, agent identities, token exchange, workload identity, and continuous verification by Veza and Astrix", { maxH: 640 }),
    ...numbered([
      "**User sign-on.** The employee signs in with Okta (OIDC). The API verifies the JWT (issuer, audience, expiry, signature from the published keys) and builds `UserContext` from its claims plus the directory. Only `api/auth.py: current_user` changes; routes do not, as ADR 0015 anticipated.",
      "**Agent identities.** Each AegisDesk agent and the approval workflow identity is registered in Okta (or Entra Agent ID when it runs in Azure) with a human owner, and linked to its Lyzr record.",
      "**Delegated tokens.** For each tool call the host performs OAuth token exchange (RFC 8693): the user’s token as subject, the agent as actor, the MCP server as audience. The result carries `sub`, `act` and `aud`, the same claims `identity/tokens.py` mints today, now signed by the IdP with asymmetric keys. Tokens are cached per user, agent and audience for their short lifetime.",
      "**Enterprise applications.** Behind MCP, Cross App Access lets the IdP issue scoped tokens to the target application on the user’s behalf under enterprise policy, instead of per-application consent screens.",
      "**Workload identity.** Pods reach their cloud’s APIs (secrets, storage, models) without static keys.",
      "**Verification.** Veza reads grants from the IdPs, clouds, databases and applications. The approval workflow asks Veza after provisioning whether the effective access matches the approved request and nothing more. Owners review agent access every quarter.",
      "**Discovery and posture.** Astrix continuously discovers agents and non-human identities, reports unregistered ones for registration in Lyzr, and alerts on stale or over-privileged credentials and anomalous use.",
    ]),
    ...callout("decision", [
      "**Okta is the workforce identity provider of record, federated with Entra.** Agent identities live where the agent runs (Okta, or Entra Agent ID in Azure), but every agent identity is linked to its Lyzr record and a human owner. Identity products decide who is calling; the AegisDesk policy engine still decides what the call may do.",
    ]),
    h2("13.5 Validate first"),
    ...callout("caution", [
      "• Token exchange with an `act` claim in Okta and Entra, and its latency per tool call.",
      "• Cross App Access support in the target applications and our MCP servers.",
      "• Status of Entra Agent ID features for our use; Okta Agent SSO registration of LangGraph-based agents.",
      "• Veza connectors for our target applications and for Kubernetes RBAC in the three clouds; Astrix discovery of MCP servers inside our clusters.",
    ]),
  );
}
