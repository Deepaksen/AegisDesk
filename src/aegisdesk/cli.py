"""AegisDesk command-line interface.

    aegisdesk config                          show effective model configuration
    aegisdesk chat "How do I clear my DNS cache?"
    aegisdesk triage "My VPN drops every 10 minutes"
    aegisdesk repeat "Suggest a name for a laptop" --runs 5 --temperature 1.0
    aegisdesk agent --as E1004 "What laptop is assigned to me?"
    aegisdesk agent --as E1004               interactive session

Every command prints the provider, model, prompt version, token usage and
latency of each model call, because those are the facts later milestones will
trace, evaluate and budget. `agent` also prints each step of the agent loop.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from langchain_core.messages import BaseMessage
from pydantic import ValidationError

from aegisdesk.agents.loop import AgentRun, ModelStep, ToolCallingAgent
from aegisdesk.agents.service_desk import build_service_desk_agent
from aegisdesk.config import Settings, get_settings
from aegisdesk.domain.repository import ServiceDeskRepository
from aegisdesk.identity.context import AuthenticationError, UserContext, authenticate
from aegisdesk.llm.allowlist import ModelNotAllowedError
from aegisdesk.llm.client import CallMetadata, LLMClient, StructuredOutputError
from aegisdesk.llm.factory import ModelConfigurationError, build_chat_model
from aegisdesk.llm.usage import TokenUsage
from aegisdesk.prompts.loader import PromptNotFoundError, load_prompt
from aegisdesk.schemas.triage import TicketTriage


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


def _print_run(run: AgentRun, settings: Settings, quiet: bool) -> None:
    if not quiet:
        for step in run.trajectory:
            if isinstance(step, ModelStep):
                wants = ", ".join(step.requested_tools) or "final answer"
                print(
                    f"  · step {step.step} model  in={step.usage.input_tokens} "
                    f"out={step.usage.output_tokens} {step.latency_ms:.0f}ms -> {wants}"
                )
            else:
                args = json.dumps(step.args)
                detail = f" ({step.error_category})" if step.error_category else ""
                print(
                    f"  · step {step.step} tool   {step.tool_name}({args}) "
                    f"{step.status.value}{detail} {step.latency_ms:.1f}ms"
                )
    print(f"Assistant: {run.answer}")
    if not quiet:
        print(
            f"  [{run.agent_name}@{run.agent_version} prompt={run.prompt_name}@"
            f"{run.prompt_version} model={settings.model_provider.value}/{settings.model_name} "
            f"stop={run.stop_reason.value} llm_calls={run.llm_calls} "
            f"tool_calls={len(run.tool_steps)} tokens={run.usage.total_tokens} "
            f"latency={run.latency_ms:.0f}ms request_id={run.request_id}]"
        )


def _agent_turn(
    agent: ToolCallingAgent,
    user: UserContext,
    text: str,
    history: list[BaseMessage],
    settings: Settings,
    quiet: bool,
) -> list[BaseMessage]:
    run = agent.run(text, user=user, history=history)
    _print_run(run, settings, quiet)
    return run.history


def cmd_agent(settings: Settings, args: argparse.Namespace) -> int:
    repository = ServiceDeskRepository.from_seed(settings.seed_data_dir)
    try:
        # Simulated login. The identity is fixed here, before the model is involved.
        user = authenticate(repository, args.employee_id)
    except AuthenticationError as exc:
        print(f"Login failed: {exc}", file=sys.stderr)
        return 2

    agent = build_service_desk_agent(settings, repository, prompt_version=args.prompt_version)
    if args.message:
        _agent_turn(agent, user, args.message, [], settings, args.quiet)
        return 0

    print(f"Signed in as {user.employee_id}. Type 'exit' to quit.")
    history: list[BaseMessage] = []
    while True:
        try:
            text = input(f"{user.employee_id}> ").strip()
        except EOFError:
            print()
            return 0
        if text.lower() in {"exit", "quit"}:
            return 0
        if text:
            history = _agent_turn(agent, user, text, history, settings, args.quiet)


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
    p.add_argument("--prompt-version", default="v1")
    p.add_argument("--quiet", action="store_true", help="print only the answer")
    return parser


COMMANDS = {
    "config": cmd_config,
    "chat": cmd_chat,
    "triage": cmd_triage,
    "repeat": cmd_repeat,
    "agent": cmd_agent,
}


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return COMMANDS[args.command](get_settings(), args)
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
