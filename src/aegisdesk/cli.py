"""Milestone 0 command-line interface.

    aegisdesk config                          show effective model configuration
    aegisdesk chat "How do I clear my DNS cache?"
    aegisdesk triage "My VPN drops every 10 minutes"
    aegisdesk repeat "Suggest a name for a laptop" --runs 5 --temperature 1.0

Every command prints the provider, model, prompt version, token usage and
latency of each model call, because those are the facts later milestones will
trace, evaluate and budget.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence

from pydantic import ValidationError

from aegisdesk.config import Settings, get_settings
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aegisdesk", description="AegisDesk Milestone 0 CLI")
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
    return parser


COMMANDS = {"config": cmd_config, "chat": cmd_chat, "triage": cmd_triage, "repeat": cmd_repeat}


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
