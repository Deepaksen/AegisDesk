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

Every command prints the provider, model, prompt version, token usage and
latency of each model call, because those are the facts later milestones will
trace, evaluate and budget. `agent` also prints each step of the agent loop.
"""

from __future__ import annotations

import argparse
import json
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
from aegisdesk.config import (
    PROJECT_ROOT,
    Settings,
    ToolTransport,
    VectorStoreKind,
    get_settings,
)
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.evals.retrieval import RagDataset, evaluate_retrieval
from aegisdesk.graphs.service_desk_graph import (
    NODE_START,
    ThreadAccessError,
    ThreadedGraphAgent,
    step_from_entry,
)
from aegisdesk.identity.context import AuthenticationError, UserContext, authenticate
from aegisdesk.llm.allowlist import ModelNotAllowedError
from aegisdesk.llm.client import CallMetadata, LLMClient, StructuredOutputError
from aegisdesk.llm.factory import ModelConfigurationError, build_chat_model
from aegisdesk.llm.usage import TokenUsage
from aegisdesk.mcp_servers.catalogue import McpServerName, build_servers
from aegisdesk.mcp_servers.server import RISK_META_KEY
from aegisdesk.persistence.checkpointer import sqlite_checkpointer
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
    if not quiet:
        thread = f" thread_id={run.thread_id}" if run.thread_id else ""
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
    repository = ServiceDeskRepository.from_seed(settings.seed_data_dir)
    # Simulated login. The identity is fixed here, before the model is involved.
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
        sqlite_checkpointer(settings.checkpoint_db_path) as checkpointer,
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
        with sqlite_checkpointer(settings.checkpoint_db_path) as checkpointer:
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
            repository, settings.mcp_token_secret.get_secret_value(), host=args.host
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
        targets = dict(build_servers(repository, "discovery-only-" + "x" * 32))
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
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](get_settings(), args)
    except AuthenticationError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 2
    except (
        ValidationError,
        ModelNotAllowedError,
        ModelConfigurationError,
        PromptNotFoundError,
    ) as exc:
        # Configuration mistakes are reported plainly; unexpected errors keep their traceback.
        print(f"Configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
