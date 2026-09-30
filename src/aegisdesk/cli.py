"""AegisDesk command-line interface.

    aegisdesk config                          show effective model configuration
    aegisdesk chat "How do I clear my DNS cache?"
    aegisdesk triage "My VPN drops every 10 minutes"
    aegisdesk repeat "Suggest a name for a laptop" --runs 5 --temperature 1.0
    aegisdesk agent --as E1004 "What laptop is assigned to me?"
    aegisdesk agent --as E1004               interactive session (LangGraph, persisted)
    aegisdesk agent --as E1004 --thread T1 "..."   continue a stored thread, even after restart
    aegisdesk agent --as E1004 --engine graph "..." the single Service Desk agent (M2/M3)
    aegisdesk agent --as E1004 --engine loop "..." the Milestone 1 hand-written loop
    aegisdesk agent --as E1004 --tools mcp_inprocess "..."  enterprise tools over MCP (M5)
    aegisdesk thread T1 --as E1004           show a stored thread
    aegisdesk rag ingest                     build the knowledge-base index
    aegisdesk rag search "vpn drops" --as E1004    inspect retrieved chunks and scores
    aegisdesk ask "How do I configure VPN on macOS?" --as E1004   answer with citations
    aegisdesk eval rag                       retrieval evaluation (recall, MRR, access)
    aegisdesk mcp serve                      run the read and action MCP servers (HTTP)
    aegisdesk mcp tools                      MCP discovery: list each server's tools
    aegisdesk policy check --as E1004 --agent knowledge --tool create_ticket
    aegisdesk audit --user E1004             audit events (AUDIT_STORE=postgres to persist)
    aegisdesk approvals list --as E1010      approvals waiting for a manager (Milestone 7)
    aegisdesk approvals approve AP-0001 --as E1010 --comment "ok"   decide, then resume
    aegisdesk db init                        migrate + checkpoint tables + seed (PostgreSQL)

Every command prints the provider, model, prompt version, token usage and
latency of each model call, because those are the facts later milestones will
trace, evaluate and budget. `agent` also prints each step of the agent loop.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage
from pydantic import ValidationError

from aegisdesk.agents.loop import (
    AgentRun,
    AgentStep,
    ModelStep,
    RouteStep,
    TrajectoryStep,
)
from aegisdesk.agents.service_desk import (
    DEFAULT_PROMPT_VERSION,
    build_service_desk_agent,
    build_service_desk_graph_agent,
)
from aegisdesk.agents.supervisor import build_supervisor_agent
from aegisdesk.approvals.service import ApprovalError, ApprovalService
from aegisdesk.config import (
    PROJECT_ROOT,
    DataStoreKind,
    ModelProvider,
    Settings,
    ToolTransport,
    VectorStoreKind,
    get_settings,
)
from aegisdesk.domain.access import ApprovalRecord
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.evals.golden import GoldenDataset
from aegisdesk.evals.judge import Judge
from aegisdesk.evals.report import Pricing, Report, comparison, summary
from aegisdesk.evals.retrieval import RagDataset, evaluate_retrieval
from aegisdesk.evals.runner import CaseRun, EvalRunner, SystemConfig
from aegisdesk.governance.factory import build_audit_log, build_gateway
from aegisdesk.governance.policy import PolicyEngine, PolicyError, PolicyInput
from aegisdesk.graphs.service_desk_graph import (
    NODE_START,
    NotPausedError,
    ThreadAccessError,
    ThreadedGraphAgent,
    step_from_entry,
)
from aegisdesk.identity.agent import AgentIdentity
from aegisdesk.identity.context import AuthenticationError, UserContext, authenticate
from aegisdesk.llm.allowlist import ModelNotAllowedError
from aegisdesk.llm.client import CallMetadata, LLMClient, StructuredOutputError
from aegisdesk.llm.factory import ModelConfigurationError, build_chat_model
from aegisdesk.llm.usage import TokenUsage
from aegisdesk.mcp_servers.catalogue import McpServerName, build_servers
from aegisdesk.mcp_servers.server import RISK_META_KEY
from aegisdesk.observability import faults, langsmith, tracing
from aegisdesk.observability.logging import configure_logging
from aegisdesk.observability.redaction import pseudonym
from aegisdesk.observability.setup import (
    TelemetryExporter,
    configure_telemetry,
    tracer_provider,
    tree_exporter,
)
from aegisdesk.observability.tree import render
from aegisdesk.persistence.factory import build_repository, open_checkpointer, psycopg_url
from aegisdesk.prompts.loader import PromptNotFoundError, load_prompt
from aegisdesk.rag.answer import GroundedAnswerer
from aegisdesk.rag.factory import build_embedder, build_retriever, build_store
from aegisdesk.rag.ingestion.pipeline import ingest_directory
from aegisdesk.schemas.triage import TicketTriage
from aegisdesk.tools.remote import McpGateway, McpTarget, ToolTransportError
from aegisdesk.tools.transport import ToolFactory


def _client(settings: Settings) -> LLMClient:
    model = build_chat_model(settings)
    return LLMClient(model, provider=settings.model_provider, model_name=settings.model_name)


def _format_metadata(meta: CallMetadata) -> str:
    return (
        f"[{meta.provider}/{meta.model} prompt={meta.prompt_name}@{meta.prompt_version} "
        f"tokens in={meta.usage.input_tokens} out={meta.usage.output_tokens} "
        f"total={meta.usage.total_tokens} latency={meta.latency_ms:.0f}ms]"
    )


def _show_messages(settings: Settings, prompt_name: str, version: str, text: str) -> None:
    prompt = load_prompt(settings.prompts_dir, prompt_name, version)
    print("--- messages sent to the model ---")
    for message in LLMClient.build_messages(prompt, text):
        print(f"{message.type:>6}: {message.text}")
    print("----------------------------------")


def cmd_config(settings: Settings, _: argparse.Namespace) -> int:
    shown = settings.model_dump(mode="json")
    # SecretStr already dumps as "**********"; make "not set" explicit too.
    shown["anthropic_api_key"] = "set" if settings.anthropic_api_key else "not set"
    print(json.dumps(shown, indent=2))
    return 0


def cmd_chat(settings: Settings, args: argparse.Namespace) -> int:
    if args.show_messages:
        _show_messages(settings, "assistant", args.prompt_version, args.message)
    prompt = load_prompt(settings.prompts_dir, "assistant", args.prompt_version)
    response = _client(settings).chat(prompt, args.message)
    print(response.text)
    print(_format_metadata(response.metadata))
    return 0


def cmd_triage(settings: Settings, args: argparse.Namespace) -> int:
    if args.show_messages:
        _show_messages(settings, "triage", args.prompt_version, args.message)
    prompt = load_prompt(settings.prompts_dir, "triage", args.prompt_version)
    try:
        result = _client(settings).structured(prompt, args.message, TicketTriage)
    except StructuredOutputError as exc:
        print(f"Structured output failed: {exc}", file=sys.stderr)
        return 2
    print(result.value.model_dump_json(indent=2))
    print(_format_metadata(result.metadata))
    return 0


def cmd_repeat(settings: Settings, args: argparse.Namespace) -> int:
    if args.temperature is not None:
        settings = settings.model_copy(update={"model_temperature": args.temperature})
    prompt = load_prompt(settings.prompts_dir, "assistant", args.prompt_version)
    client = _client(settings)

    answers: list[str] = []
    total = TokenUsage()
    for run in range(1, args.runs + 1):
        response = client.chat(prompt, args.message)
        answers.append(response.text)
        total = total + response.metadata.usage
        print(f"#{run}: {response.text}")
        print(f"    {_format_metadata(response.metadata)}")

    print(
        f"\n{len(set(answers))} distinct answer(s) from {args.runs} run(s) at "
        f"temperature={settings.model_temperature}; total tokens={total.total_tokens}"
    )
    return 0


def _format_step(step: TrajectoryStep) -> str:
    if isinstance(step, RouteStep):
        if step.error:
            decision = f"routing failed ({step.error})"
        elif step.out_of_scope:
            decision = "out of scope"
        else:
            decision = ", ".join(f"{agent}: {instr!r}" for agent, instr in step.tasks)
        return (
            f"→ router  in={step.usage.input_tokens} out={step.usage.output_tokens} "
            f"{step.latency_ms:.0f}ms -> {decision}"
        )
    if isinstance(step, AgentStep):
        note = f" [{step.note}]" if step.note else ""
        return f"← {step.agent} {step.status}{note}"
    who = f"[{step.agent}] " if step.agent else ""
    if isinstance(step, ModelStep):
        wants = ", ".join(step.requested_tools) or "final answer"
        return (
            f"  · {who}step {step.step} model  in={step.usage.input_tokens} "
            f"out={step.usage.output_tokens} {step.latency_ms:.0f}ms -> {wants}"
        )
    detail = f" ({step.error_category})" if step.error_category else ""
    return (
        f"  · {who}step {step.step} tool   {step.tool_name}({json.dumps(step.args)}) "
        f"{step.status.value}{detail} {step.latency_ms:.1f}ms"
    )


def _print_run(run: AgentRun, settings: Settings, *, quiet: bool, show_steps: bool) -> None:
    if show_steps and not quiet:
        for step in run.trajectory:
            print(_format_step(step))
    print(f"Assistant: {run.answer}")
    if run.pending_approvals:
        steps = ", ".join(
            f"{p['approval_id']} ({p['step']}: {p['approver']})" for p in run.pending_approvals
        )
        print(f"⏸ Waiting for approval: {steps}. Thread {run.thread_id} will resume on decision.")
    if not quiet:
        thread = f" thread_id={run.thread_id}" if run.thread_id else ""
        thread += f" trace_id={run.trace_id}" if run.trace_id else ""
        print(
            f"  [{run.agent_name}@{run.agent_version} prompt={run.prompt_name}@"
            f"{run.prompt_version} model={settings.model_provider.value}/{settings.model_name} "
            f"stop={run.stop_reason.value} llm_calls={run.llm_calls} "
            f"tool_calls={len(run.tool_steps)} tokens={run.usage.total_tokens} "
            f"latency={run.latency_ms:.0f}ms request_id={run.request_id}{thread}]"
        )


class _StepPrinter:
    """Prints trajectory entries live, as each graph node finishes (streaming)."""

    def __init__(self) -> None:
        self._printed = 0

    def __call__(self, node: str, update: dict[str, Any]) -> None:
        trajectory = update.get("trajectory")
        if trajectory is None:
            return
        if node == NODE_START:
            self._printed = 0
            return
        for entry in trajectory[self._printed :]:
            print(_format_step(step_from_entry(entry)), flush=True)
        self._printed = len(trajectory)


def _read_turns(prompt: str) -> Iterator[str]:
    while True:
        try:
            text = input(prompt).strip()
        except EOFError:
            print()
            return
        if text.lower() in {"exit", "quit"}:
            return
        if text:
            yield text


def _login(settings: Settings, employee_id: str) -> tuple[ServiceDeskRepository, UserContext]:
    repository = build_repository(settings)
    # Simulated login. The identity is fixed here, before the model is involved.
    with tracing.span("aegisdesk.authenticate", **{tracing.USER: pseudonym(employee_id)}):
        return repository, authenticate(repository, employee_id)


def _run_loop_engine(
    settings: Settings,
    repository: ServiceDeskRepository,
    user: UserContext,
    args: argparse.Namespace,
) -> int:
    agent = build_service_desk_agent(settings, repository, prompt_version=args.prompt_version)
    if args.message:
        run = agent.run(args.message, user=user)
        _print_run(run, settings, quiet=args.quiet, show_steps=True)
        return 0

    print(f"Signed in as {user.employee_id} (engine: loop). Type 'exit' to quit.")
    history: list[BaseMessage] = []
    for text in _read_turns(f"{user.employee_id}> "):
        run = agent.run(text, user=user, history=history)
        _print_run(run, settings, quiet=args.quiet, show_steps=True)
        history = run.history
    return 0


def _run_graph_engine(
    settings: Settings,
    repository: ServiceDeskRepository,
    user: UserContext,
    args: argparse.Namespace,
) -> int:
    thread_id = args.thread or str(uuid.uuid4())
    if args.tools is not None:
        settings = settings.model_copy(update={"tool_transport": ToolTransport(args.tools)})
    if args.engine != "multi" and settings.tool_transport is not ToolTransport.LOCAL:
        print("Note: MCP tool transport applies to --engine multi; using local tools.")
        settings = settings.model_copy(update={"tool_transport": ToolTransport.LOCAL})
    with (
        open_checkpointer(settings) as checkpointer,
        ToolFactory.from_settings(settings, repository) as tool_factory,
    ):
        agent: ThreadedGraphAgent
        if args.engine == "multi":
            if not args.quiet:
                print(f"Tools: {settings.tool_transport.value}")
            agent = build_supervisor_agent(
                settings, repository, checkpointer=checkpointer, tool_factory=tool_factory
            )
        else:
            agent = build_service_desk_graph_agent(
                settings, repository, checkpointer=checkpointer, prompt_version=args.prompt_version
            )
        printer = None if args.quiet else _StepPrinter()

        def turn(text: str) -> None:
            run = agent.run(text, user=user, thread_id=thread_id, on_update=printer)
            _print_run(run, settings, quiet=args.quiet, show_steps=False)
            _print_trace(run.trace_id)

        if args.message:
            turn(args.message)
            return 0

        previous = len(agent.history(thread_id, user))
        resumed = f", resuming {previous} stored messages" if previous else ""
        print(f"Signed in as {user.employee_id}. Thread {thread_id}{resumed}. Type 'exit' to quit.")
        for text in _read_turns(f"{user.employee_id}> "):
            turn(text)
    return 0


def cmd_agent(settings: Settings, args: argparse.Namespace) -> int:
    try:
        repository, user = _login(settings, args.employee_id)
        if args.engine == "loop":
            return _run_loop_engine(settings, repository, user, args)
        return _run_graph_engine(settings, repository, user, args)
    except AuthenticationError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 2
    except ThreadAccessError as exc:
        print(f"Access denied: {exc}", file=sys.stderr)
        return 2


def cmd_thread(settings: Settings, args: argparse.Namespace) -> int:
    try:
        repository, user = _login(settings, args.employee_id)
        with open_checkpointer(settings) as checkpointer:
            agent = build_service_desk_graph_agent(settings, repository, checkpointer=checkpointer)
            messages = agent.history(args.thread_id, user)
    except AuthenticationError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 2
    except ThreadAccessError as exc:
        print(f"Access denied: {exc}", file=sys.stderr)
        return 2

    if not messages:
        print(f"No stored messages for thread {args.thread_id}.")
        return 1
    for message in messages:
        text: str = message.text
        if isinstance(message, AIMessage) and message.tool_calls:
            calls = ", ".join(f"{c['name']}({json.dumps(c['args'])})" for c in message.tool_calls)
            text = f"{text} [tool calls: {calls}]".strip()
        if len(text) > 160:
            text = text[:157] + "..."
        print(f"{message.type:>5}: {text}")
    return 0


def cmd_rag(settings: Settings, args: argparse.Namespace) -> int:
    if args.rag_command == "ingest":
        return _rag_ingest(settings, args)
    return _rag_search(settings, args)


def _rag_ingest(settings: Settings, args: argparse.Namespace) -> int:
    embedder = build_embedder(settings)
    store = build_store(settings)
    if args.rebuild:
        store.reset()
    report = ingest_directory(settings.documents_dir, embedder, store)
    print(
        f"Ingested {settings.documents_dir} into {settings.vector_store.value} store "
        f"with {report.embedding_model} in {report.seconds:.2f}s"
    )
    for label, ids in (
        ("added", report.added),
        ("updated", report.updated),
        ("unchanged", report.unchanged),
        ("removed", report.removed),
    ):
        if ids:
            print(f"  {label:<9} {len(ids):>2}: {', '.join(ids)}")
    print(f"  chunks written: {report.chunks_written}; total in store: {store.chunk_count()}")
    if settings.vector_store is VectorStoreKind.MEMORY:
        print("  (in-memory store: the index lives only for this process)")
    return 0


def _rag_search(settings: Settings, args: argparse.Namespace) -> int:
    _, user = _login(settings, args.employee_id)
    retriever = build_retriever(settings)
    result = retriever.retrieve(args.query, user, top_k=args.k)
    print(
        f"top_k={result.top_k} min_score={result.min_score} embedder={retriever.embedding_model} "
        f"latency={result.latency_ms:.1f}ms user={user.employee_id} roles={list(user.roles)}"
    )
    if not result.chunks:
        print("No chunk reached the evidence threshold: insufficient evidence.")
    ranked = [(s, True) for s in result.chunks] + [(s, False) for s in result.below_threshold]
    for rank, (scored, usable) in enumerate(ranked, start=1):
        chunk, meta = scored.chunk, scored.chunk.metadata
        flag = "" if usable else "  (below threshold, not used)"
        print(
            f"\n#{rank} {scored.score:.3f} {chunk.chunk_id} | {meta.title} v{meta.version} "
            f"| {chunk.section} | {meta.classification.value}{flag}"
        )
        body = chunk.text.split("\n", 1)[-1].replace("\n", " ")
        print(f"   {body[:220]}{'...' if len(body) > 220 else ''}")
    return 0


def cmd_ask(settings: Settings, args: argparse.Namespace) -> int:
    _, user = _login(settings, args.employee_id)
    answerer = GroundedAnswerer(
        build_retriever(settings),
        _client(settings),
        load_prompt(settings.prompts_dir, "grounded_answer", args.prompt_version),
    )
    result = answerer.answer(args.question, user)
    print(result.answer)
    if result.citations:
        print("\nSources:")
        for c in result.citations:
            print(f"  [{c.chunk_id}] {c.title} v{c.version} - {c.section}")
    retrieved = ", ".join(f"{s.chunk.chunk_id}({s.score:.2f})" for s in result.retrieval.chunks)
    llm = _format_metadata(result.llm) if result.llm else "[model not called]"
    print(
        f"\n  status={result.status.value} retrieved=[{retrieved}] "
        f"retrieval={result.retrieval.latency_ms:.1f}ms"
    )
    if result.rejected_citations:
        print(f"  rejected citations (not retrieved): {result.rejected_citations}")
    print(f"  {llm}")
    return 0


def cmd_eval(settings: Settings, args: argparse.Namespace) -> int:
    if args.eval_command == "golden":
        return _eval_golden(settings, args)
    dataset = RagDataset.load(args.dataset)
    repository = ServiceDeskRepository.from_seed(settings.seed_data_dir)
    report = evaluate_retrieval(dataset, build_retriever(settings), repository)
    print(
        f"{report.dataset} v{report.dataset_version}: {len(report.results)} cases | "
        f"embedder={report.embedding_model} top_k={report.top_k} min_score={report.min_score}"
    )
    print(f"  hit rate@k            {report.hit_rate:.2f}")
    print(f"  recall@k              {report.recall:.2f}")
    print(f"  MRR                   {report.mrr:.2f}")
    print(f"  no-evidence accuracy  {report.no_evidence_accuracy:.2f}")
    print(f"  access violations     {report.access_violations}")
    print(f"  mean latency          {report.mean_latency_ms:.1f}ms")
    for failure in report.failures:
        case = failure.case
        print(
            f"  FAIL {case.id} ({case.category}) as {case.user}: {case.question!r} -> "
            f"retrieved {failure.retrieved_document_ids or 'nothing'}, "
            f"expected {case.expected_document_ids or 'nothing'}"
            + (f", LEAKED {failure.access_violations}" if failure.access_violations else "")
        )
    gate_failed = report.access_violations > 0 or (
        args.min_hit_rate is not None and report.hit_rate < args.min_hit_rate
    )
    return 1 if gate_failed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aegisdesk", description="AegisDesk CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("config", help="show effective model configuration (secrets masked)")

    for name, help_text in (
        ("chat", "plain chat completion"),
        ("triage", "structured output: classify a service-desk message"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("message")
        p.add_argument("--prompt-version", default="v1")
        p.add_argument(
            "--show-messages", action="store_true", help="print the message list sent to the model"
        )

    p = sub.add_parser("repeat", help="send the same message N times to observe (non-)determinism")
    p.add_argument("message")
    p.add_argument("--runs", type=int, default=3)
    p.add_argument("--temperature", type=float, default=None)
    p.add_argument("--prompt-version", default="v1")

    p = sub.add_parser("agent", help="talk to the Service Desk agent (tools + agent loop)")
    p.add_argument("message", nargs="?", help="a single request; omit for an interactive session")
    p.add_argument(
        "--as",
        dest="employee_id",
        required=True,
        help="employee ID to sign in as (simulated authentication), e.g. E1004",
    )
    p.add_argument(
        "--prompt-version",
        default=DEFAULT_PROMPT_VERSION,
        help="v2 (default): with knowledge base; v1: the Milestone 1 agent",
    )
    p.add_argument("--quiet", action="store_true", help="print only the answer")
    p.add_argument(
        "--engine",
        choices=["multi", "graph", "loop"],
        default="multi",
        help=(
            "multi: supervisor + specialist agents (default); graph: the single Service Desk "
            "agent (M2/M3); loop: the M1 hand-written loop"
        ),
    )
    p.add_argument("--thread", help="thread ID to continue (graph engine); a new one if omitted")
    p.add_argument(
        "--trace", action="store_true", help="print this run's trace as a span tree (Milestone 8)"
    )
    p.add_argument(
        "--tools",
        choices=[t.value for t in ToolTransport],
        default=None,
        help="where enterprise tools run (default TOOL_TRANSPORT); multi engine only",
    )

    rag = sub.add_parser("rag", help="knowledge base: ingest documents, inspect retrieval")
    rag_sub = rag.add_subparsers(dest="rag_command", required=True)
    p = rag_sub.add_parser("ingest", help="load, chunk, embed and store the documents")
    p.add_argument("--rebuild", action="store_true", help="drop the index and re-embed everything")
    p = rag_sub.add_parser("search", help="show the chunks retrieved for a query, with scores")
    p.add_argument("query")
    p.add_argument("--as", dest="employee_id", required=True, help="employee ID (access filter)")
    p.add_argument("--k", type=int, default=None, help="top-k (default RAG_TOP_K)")

    p = sub.add_parser("ask", help="answer a question from the documents, with citations")
    p.add_argument("question")
    p.add_argument("--as", dest="employee_id", required=True, help="employee ID (access filter)")
    p.add_argument("--prompt-version", default="v1")

    ev = sub.add_parser("eval", help="run an evaluation suite")
    ev_sub = ev.add_subparsers(dest="eval_command", required=True)
    p = ev_sub.add_parser("golden", help="golden dataset: agents, tools, policy, RAG (M9)")
    p.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "evals/datasets/golden_v1.yaml")
    p.add_argument("--config", choices=[c.value for c in SystemConfig], default="multi")
    p.add_argument("--compare", choices=[c.value for c in SystemConfig], help="second config")
    p.add_argument("--judge", action="store_true", help="LLM judge (EVAL_JUDGE_PROVIDER/MODEL)")
    p.add_argument("--quality-gate", action="store_true", help="enforce the spec's targets")
    p.add_argument("--baseline", type=Path, default=None, help="fail if metrics drop below it")
    p.add_argument("--write-baseline", type=Path, default=None, help="store this run as baseline")
    p.add_argument("--out", type=Path, default=None, help="results JSON path")
    p = ev_sub.add_parser("rag", help="deterministic retrieval evaluation")
    p.add_argument("--dataset", type=Path, default=PROJECT_ROOT / "evals/datasets/rag_v1.yaml")
    p.add_argument("--min-hit-rate", type=float, default=None, help="fail if hit rate is lower")

    mcp = sub.add_parser("mcp", help="MCP servers for the enterprise tools (Milestone 5)")
    mcp_sub = mcp.add_subparsers(dest="mcp_command", required=True)
    p = mcp_sub.add_parser("serve", help="serve the read and action MCP servers over HTTP")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p = mcp_sub.add_parser("tools", help="discover the tools each MCP server offers")
    p.add_argument(
        "--remote",
        action="store_true",
        help="query the HTTP servers (MCP_READ_URL/MCP_ACTION_URL) instead of in-process",
    )

    pol = sub.add_parser("policy", help="governance policy (Milestone 6)")
    pol_sub = pol.add_subparsers(dest="policy_command", required=True)
    p = pol_sub.add_parser("check", help="ask the policy engine for one decision")
    p.add_argument("--as", dest="employee_id", required=True, help="employee ID")
    p.add_argument("--agent", required=True, help="agent id, e.g. knowledge, access")
    p.add_argument("--tool", required=True)
    p.add_argument("--environment", help="where the call is enforced (default AEGIS_ENV)")
    p.add_argument("--agent-environment", help="the agent's claimed environment")

    p = sub.add_parser("audit", help="list audit events (use AUDIT_STORE=postgres)")
    p.add_argument("--request", help="only this request ID")
    p.add_argument("--user", help="only this employee ID")
    p.add_argument("--limit", type=int, default=50)

    ap = sub.add_parser("approvals", help="approve or reject access requests (Milestone 7)")
    ap_sub = ap.add_subparsers(dest="approvals_command", required=True)
    p = ap_sub.add_parser("list", help="approvals waiting for you")
    p.add_argument("--as", dest="employee_id", required=True, help="approver's employee ID")
    p = ap_sub.add_parser("show", help="one approval")
    p.add_argument("approval_id")
    p.add_argument("--as", dest="employee_id", required=True)
    for verb in ("approve", "reject"):
        p = ap_sub.add_parser(verb, help=f"{verb} an approval step, then resume the workflow")
        p.add_argument("approval_id")
        p.add_argument("--as", dest="employee_id", required=True, help="approver's employee ID")
        p.add_argument("--comment", default=None)

    db = sub.add_parser("db", help="database setup (PostgreSQL)")
    db_sub = db.add_subparsers(dest="db_command", required=True)
    db_sub.add_parser("init", help="migrate, create checkpoint tables, load seed rows")
    db_sub.add_parser("seed", help="load seed rows that are missing")

    sub.add_parser("telemetry", help="show where traces, metrics and logs go (Milestone 8)")

    p = sub.add_parser("thread", help="show a stored conversation thread")
    p.add_argument("thread_id")
    p.add_argument("--as", dest="employee_id", required=True, help="employee ID to sign in as")
    return parser


def cmd_mcp(settings: Settings, args: argparse.Namespace) -> int:
    repository = ServiceDeskRepository.from_seed(settings.seed_data_dir)
    if args.mcp_command == "serve":
        if settings.mcp_token_secret is None:
            print("Configuration error: set MCP_TOKEN_SECRET (32+ characters)", file=sys.stderr)
            return 2
        import uvicorn

        from aegisdesk.mcp_servers.http import build_http_app

        app = build_http_app(
            repository,
            settings.mcp_token_secret.get_secret_value(),
            gateway=build_gateway(settings),
            host=args.host,
        )
        print(f"MCP servers: http://{args.host}:{args.port}/read/mcp, /action/mcp")
        uvicorn.run(app, host=args.host, port=args.port, log_level="info")
        return 0

    # Discovery needs no token: listing tools reveals no user data.
    targets: dict[McpServerName, McpTarget]
    if args.remote:
        targets = {
            McpServerName.READ: settings.mcp_read_url,
            McpServerName.ACTION: settings.mcp_action_url,
        }
    else:
        targets = dict(
            build_servers(repository, "discovery-only-" + "x" * 32, gateway=build_gateway(settings))
        )
    with McpGateway(targets, timeout_seconds=settings.mcp_timeout_seconds) as gateway:
        for server in McpServerName:
            try:
                tools = gateway.list_tools(server)
            except ToolTransportError as exc:
                print(f"[{server}] {exc}", file=sys.stderr)
                return 1
            print(f"[{server}] audience={server.audience}")
            for tool in tools:
                meta = tool.meta or {}
                hints = tool.annotations
                flags = [
                    "read-only" if hints and hints.read_only_hint else "write",
                    "idempotent" if hints and hints.idempotent_hint else "non-idempotent",
                ]
                print(f"  {tool.name:<26} risk={meta.get(RISK_META_KEY)} {', '.join(flags)}")
    return 0


def cmd_policy(settings: Settings, args: argparse.Namespace) -> int:
    """Ask the policy engine directly, without any model or tool involved."""
    repository = ServiceDeskRepository.from_seed(settings.seed_data_dir)
    user = authenticate(repository, args.employee_id)
    engine = PolicyEngine.from_file(settings.policy_path)
    environment = args.environment or settings.aegis_env.value
    agent = AgentIdentity(
        agent_id=args.agent,
        agent_version="cli",
        agent_type="specialist",
        environment=args.agent_environment or environment,
    )
    decision = engine.evaluate(
        PolicyInput(tool=args.tool, user=user, agent=agent, environment=environment)
    )
    risk = decision.risk.value if decision.risk else "unclassified"
    print(
        f"{decision.decision.value.upper()}  tool={args.tool} agent={args.agent} "
        f"user={user.employee_id} environment={environment} risk={risk}"
    )
    for reason in decision.reasons:
        print(f"  reason: {reason}")
    print(f"  policy: {decision.policy_version} ({settings.policy_path.name})")
    return 0 if decision.allowed else 1


def cmd_audit(settings: Settings, args: argparse.Namespace) -> int:
    audit = build_audit_log(settings)
    events = audit.query(request_id=args.request, user_id=args.user, limit=args.limit)
    if not events:
        store = settings.audit_store.value
        print(f"No audit events (AUDIT_STORE={store}; the memory store is per process).")
        return 0
    for e in events:
        agent = f"{e.agent_id}@{e.agent_version}" if e.agent_id else "-"
        reasons = f" reasons={','.join(e.policy_reasons)}" if e.policy_reasons else ""
        resource = f" resource={json.dumps(e.resource)}" if e.resource else ""
        if e.approval_id:
            resource += f" approval={e.approval_id} approver={e.approver_id}"
        print(
            f"{e.occurred_at:%Y-%m-%d %H:%M:%S} {e.phase.value:<8} {e.tool:<24} "
            f"user={e.user_id} agent={agent} env={e.environment} "
            f"decision={e.policy_decision} outcome={e.outcome}{reasons}{resource} "
            f"request_id={e.request_id}" + (f" thread_id={e.thread_id}" if e.thread_id else "")
        )
    return 0


def _golden_report(
    settings: Settings, dataset: GoldenDataset, config: SystemConfig, judge: bool
) -> Report:
    model_label = f"{settings.model_provider.value}/{settings.model_name}"
    runner = EvalRunner(
        settings,
        build_retriever(settings),
        today=dataset.today,
        model_factory=lambda: build_chat_model(settings),
        model_label=model_label,
    )
    runs = []
    for case in dataset.cases:
        runs.append(runner.run(case, config))
    report = Report.build(
        dataset.name,
        dataset.version,
        config.value,
        model_label,
        runs,
        Pricing.load(PROJECT_ROOT / "config" / "pricing.yaml"),
    )
    if judge:
        report.judge = _judge(settings, runs)
    return report


def _judge(settings: Settings, runs: list[CaseRun]) -> dict[str, Any]:
    provider = os.environ.get("EVAL_JUDGE_PROVIDER")
    name = os.environ.get("EVAL_JUDGE_MODEL")
    if not provider or not name:
        return {"model": None, "status": "not run: set EVAL_JUDGE_PROVIDER and EVAL_JUDGE_MODEL"}
    judge_settings = settings.model_copy(
        update={"model_provider": ModelProvider(provider), "model_name": name}
    )
    judge = Judge(
        build_chat_model(judge_settings),
        load_prompt(settings.prompts_dir, "judge", "v1"),
        provider=ModelProvider(provider),
        model_name=name,
    )
    return judge.grade_all(runs)


def _eval_golden(settings: Settings, args: argparse.Namespace) -> int:
    dataset = GoldenDataset.load(args.dataset)
    report = _golden_report(settings, dataset, SystemConfig(args.config), args.judge)
    print(summary(report))
    out = args.out or (
        PROJECT_ROOT
        / "evals"
        / "results"
        / f"{dataset.name}-v{dataset.version}-{report.config}-{settings.model_name}.json"
    )
    report.write(out)
    print(f"  results: {out}")

    if args.compare:
        other = _golden_report(settings, dataset, SystemConfig(args.compare), False)
        other.write(out.with_name(out.stem.replace(report.config, other.config) + ".json"))
        print()
        print(comparison(report, other))

    failures = [f"safety: {f}" for f in report.safety_failures()]
    if args.quality_gate:
        failures += [f"quality: {f}" for f in report.quality_failures()]
    baseline = args.baseline
    if baseline is not None and not baseline.exists():
        failures.append(f"regression: baseline {baseline} not found")
    elif baseline is not None:
        failures += [
            f"regression: {f}"
            for f in report.regression_failures(json.loads(baseline.read_text(encoding="utf-8")))
        ]
    if args.write_baseline is not None:
        report.write(args.write_baseline)
        print(f"  baseline written: {args.write_baseline}")
    for failure in failures:
        print(f"  GATE FAILED {failure}")
    if not failures:
        print(
            "  gates passed"
            + (" (incl. quality)" if args.quality_gate else " (safety, regression)")
        )
    return 1 if failures else 0


def _print_trace(trace_id: str | None) -> None:
    """With --trace (TELEMETRY_EXPORTER=tree): the run's spans as a tree."""
    exporter = tree_exporter()
    if exporter is None or trace_id is None:
        return
    tracer_provider().force_flush()
    print(render(exporter.spans, int(trace_id, 16)))


def _approval_service(settings: Settings, repository: ServiceDeskRepository) -> ApprovalService:
    return ApprovalService(
        repository.access_store, build_audit_log(settings), environment=settings.aegis_env.value
    )


def cmd_approvals(settings: Settings, args: argparse.Namespace) -> int:
    """The manager's side of the approval workflow (a UI arrives in Milestone 10)."""
    repository, approver = _login(settings, args.employee_id)
    service = _approval_service(settings, repository)
    memory_note = (
        "  (DATA_STORE=memory: approvals exist only inside one process; "
        "use DATA_STORE=postgres to decide from another process.)"
    )
    try:
        if args.approvals_command == "list":
            pending = service.list_pending_for(approver)
            if not pending:
                print(f"No approvals waiting for {approver.employee_id}.")
                if settings.data_store is DataStoreKind.MEMORY:
                    print(memory_note)
            for a in pending:
                print(_format_approval(a, repository))
            return 0
        if args.approvals_command == "show":
            print(_format_approval(service.get(args.approval_id, approver), repository))
            return 0

        result = service.decide(
            args.approval_id,
            approver,
            approve=args.approvals_command == "approve",
            comment=args.comment,
        )
    except ApprovalError as exc:
        print(f"Refused ({exc.category}): {exc}", file=sys.stderr)
        return 2
    print(_format_approval(result.approval, repository))
    if not result.changed:
        print("  (already recorded; nothing changed)")
    if result.request.status.is_open:
        print(f"  {result.request.request_id} still waits for other approvals.")
        return 0
    return _resume_thread(settings, repository, result.request.thread_id)


def _resume_thread(
    settings: Settings, repository: ServiceDeskRepository, thread_id: str | None
) -> int:
    if thread_id is None:
        print("  No conversation to resume (the request was not made through the assistant).")
        return 0
    with (
        open_checkpointer(settings) as checkpointer,
        ToolFactory.from_settings(settings, repository) as tool_factory,
    ):
        agent = build_supervisor_agent(
            settings, repository, checkpointer=checkpointer, tool_factory=tool_factory
        )
        try:
            run = agent.resume(thread_id)
        except NotPausedError:
            print(f"  Thread {thread_id} is not paused (already resumed).")
            return 0
    print(f"Resumed thread {thread_id}:")
    print(f"Assistant: {run.answer}")
    _print_trace(run.trace_id)
    return 0


def _format_approval(a: ApprovalRecord, repository: ServiceDeskRepository) -> str:
    requester = repository.get_employee(a.requester_id)
    app = repository.get_application(a.application_id)
    who = a.approver_id or f"any {a.approver_role}"
    decided = (f" by {a.decided_by} at {a.decided_at:%Y-%m-%d %H:%M}" if a.decided_at else "") + (
        f' "{a.comment}"' if a.comment else ""
    )
    return (
        f"{a.approval_id}  {a.status.value:<8} {a.step.value:<10} {a.access_request_id} "
        f"{app.name if app else a.application_id} for {requester.name if requester else ''} "
        f"({a.requester_id})  approver={who}  expires={a.expires_at:%Y-%m-%d %H:%M}{decided}"
    )


def cmd_db(settings: Settings, args: argparse.Namespace) -> int:
    """Create schemas (Alembic + LangGraph checkpointer) and load seed rows. Never at startup."""
    from aegisdesk.domain.access_store_pg import PgAccessStore

    if args.db_command == "init":
        from alembic import command
        from alembic.config import Config

        command.upgrade(Config(str(PROJECT_ROOT / "alembic.ini")), "head")
        from langgraph.checkpoint.postgres import PostgresSaver

        with PostgresSaver.from_conn_string(psycopg_url(settings.database_url)) as saver:
            saver.setup()
        print("Schema up to date (Alembic head + LangGraph checkpoint tables).")
    seed = ServiceDeskRepository.from_seed(settings.seed_data_dir)
    requests = [r for e in seed.list_employee_ids() for r in seed.access_requests_for(e)]
    access = [a for e in seed.list_employee_ids() for a in seed.access_for(e)]
    inserted = PgAccessStore(settings.database_url).seed(access, requests)
    print(f"Seed rows inserted: {inserted} (existing rows are never overwritten).")
    return 0


def _configure_observability(settings: Settings, args: argparse.Namespace) -> Settings:
    if getattr(args, "trace", False):
        settings = settings.model_copy(update={"telemetry_exporter": TelemetryExporter.TREE})
    service = "aegisdesk-mcp" if args.command == "mcp" else "aegisdesk-cli"
    configure_logging(fmt=settings.log_format.value, level=settings.log_level)
    configure_telemetry(
        service_name=service,
        exporter=settings.telemetry_exporter,
        environment=settings.aegis_env.value,
    )
    return settings


def cmd_telemetry(settings: Settings, _args: argparse.Namespace) -> int:
    """Where telemetry goes, without printing any secret."""
    report = {
        "service": "aegisdesk-cli",
        "exporter": settings.telemetry_exporter.value,
        "otlp_endpoint": os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT", "(SDK default)"),
        "otlp_headers": "set" if os.environ.get("OTEL_EXPORTER_OTLP_HEADERS") else "not set",
        "log_format": settings.log_format.value,
        "log_level": settings.log_level,
        "langsmith": "on" if langsmith.enabled() else "off",
        "langsmith_project": os.environ.get("LANGSMITH_PROJECT", "(default)"),
        "faults": os.environ.get(faults.ENV) or "none",
    }
    print(json.dumps(report, indent=2))
    return 0


COMMANDS = {
    "config": cmd_config,
    "chat": cmd_chat,
    "triage": cmd_triage,
    "repeat": cmd_repeat,
    "agent": cmd_agent,
    "thread": cmd_thread,
    "rag": cmd_rag,
    "ask": cmd_ask,
    "eval": cmd_eval,
    "mcp": cmd_mcp,
    "policy": cmd_policy,
    "audit": cmd_audit,
    "approvals": cmd_approvals,
    "telemetry": cmd_telemetry,
    "db": cmd_db,
}


def _one_shot(args: argparse.Namespace) -> bool:
    if args.command == "agent":
        return bool(args.message)  # interactive sessions: one trace per turn instead
    return args.command in {"approvals", "ask", "thread"}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        settings = _configure_observability(get_settings(), args)
        if _one_shot(args):
            # One trace for the whole command: authentication, the run, the resume.
            with tracing.span(f"aegisdesk.cli {args.command}"):
                return COMMANDS[args.command](settings, args)
        return COMMANDS[args.command](settings, args)
    except AuthenticationError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 2
    except (
        ValidationError,
        ModelNotAllowedError,
        ModelConfigurationError,
        PromptNotFoundError,
        PolicyError,
    ) as exc:
        # Configuration mistakes are reported plainly; unexpected errors keep their traceback.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
