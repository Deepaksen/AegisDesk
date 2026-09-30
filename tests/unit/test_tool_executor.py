"""Executor behaviour independent of any particular tool."""

from __future__ import annotations

import json
import logging

import pytest
from pydantic import BaseModel, ConfigDict

from aegisdesk.identity.context import UserContext
from aegisdesk.tools.base import (
    NotFoundError,
    ToolAccess,
    ToolCallContext,
    ToolRisk,
    ToolSpec,
)
from aegisdesk.tools.executor import OutcomeStatus, ToolExecutor, idempotency_key

USER = UserContext(employee_id="E1", roles=("employee",), department="it", manager_id=None)


class EchoInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str
    secret_hint: str = ""


class EchoOutput(BaseModel):
    text: str
    seen_user: str
    idempotency_key: str | None


def _spec(handler, *, access: ToolAccess = ToolAccess.READ) -> ToolSpec[EchoInput, EchoOutput]:  # type: ignore[no-untyped-def]
    return ToolSpec(
        name="echo",
        description="Echo text.",
        input_model=EchoInput,
        output_model=EchoOutput,
        handler=handler,
        risk=ToolRisk.LOW,
        access=access,
        idempotent=True,
        owner="tests",
    )


def _echo(args: EchoInput, ctx: ToolCallContext) -> EchoOutput:
    return EchoOutput(
        text=args.text, seen_user=ctx.user.employee_id, idempotency_key=ctx.idempotency_key
    )


def _error(content: str) -> dict[str, str]:
    error: dict[str, str] = json.loads(content)["error"]
    return error


def test_successful_call_gets_trusted_user() -> None:
    outcome = ToolExecutor([_spec(_echo)]).execute(
        "echo", {"text": "hi"}, user=USER, request_id="r"
    )
    assert outcome.status is OutcomeStatus.OK
    assert json.loads(outcome.content) == {"text": "hi", "seen_user": "E1", "idempotency_key": None}
    assert outcome.latency_ms >= 0


def test_unknown_tool_is_refused() -> None:
    outcome = ToolExecutor([_spec(_echo)]).execute("rm_rf", {}, user=USER, request_id="r")
    assert outcome.status is OutcomeStatus.ERROR
    assert outcome.error_category == "unknown_tool"


def test_invalid_arguments_are_described_without_echoing_values() -> None:
    outcome = ToolExecutor([_spec(_echo)]).execute(
        "echo", {"text": 1, "extra": "sk-leak-me"}, user=USER, request_id="r"
    )
    error = _error(outcome.content)
    assert error["category"] == "invalid_arguments"
    assert "text" in error["message"] and "extra" in error["message"]
    assert "sk-leak-me" not in outcome.content


def test_expected_tool_errors_are_passed_to_the_model() -> None:
    def handler(args: EchoInput, ctx: ToolCallContext) -> EchoOutput:
        raise NotFoundError("No such thing.")

    outcome = ToolExecutor([_spec(handler)]).execute(
        "echo", {"text": "x"}, user=USER, request_id="r"
    )
    assert _error(outcome.content) == {"category": "not_found", "message": "No such thing."}


def test_unexpected_exceptions_are_hidden_from_the_model(caplog: pytest.LogCaptureFixture) -> None:
    def handler(args: EchoInput, ctx: ToolCallContext) -> EchoOutput:
        raise RuntimeError("db password=hunter2 at 10.0.0.5")

    with caplog.at_level(logging.ERROR):
        outcome = ToolExecutor([_spec(handler)]).execute(
            "echo", {"text": "x"}, user=USER, request_id="r"
        )

    assert outcome.error_category == "internal_error"
    assert "hunter2" not in outcome.content and "10.0.0.5" not in outcome.content
    assert "hunter2" in caplog.text  # still available to operators


def test_output_must_match_the_output_schema() -> None:
    def handler(args: EchoInput, ctx: ToolCallContext) -> EchoOutput:
        return {"text": "not a model"}  # type: ignore[return-value]

    outcome = ToolExecutor([_spec(handler)]).execute(
        "echo", {"text": "x"}, user=USER, request_id="r"
    )
    assert outcome.error_category == "invalid_output"


def test_write_tools_receive_an_idempotency_key() -> None:
    executor = ToolExecutor([_spec(_echo, access=ToolAccess.WRITE)])
    outcome = executor.execute("echo", {"text": "hi"}, user=USER, request_id="req-1")
    expected = idempotency_key("E1", "req-1", "echo", EchoInput(text="hi"))
    assert json.loads(outcome.content)["idempotency_key"] == expected


def test_idempotency_key_depends_on_user_request_tool_and_args() -> None:
    base = idempotency_key("E1", "r1", "t", EchoInput(text="a"))
    assert base == idempotency_key("E1", "r1", "t", EchoInput(text="a"))
    assert base != idempotency_key("E2", "r1", "t", EchoInput(text="a"))
    assert base != idempotency_key("E1", "r2", "t", EchoInput(text="a"))
    assert base != idempotency_key("E1", "r1", "u", EchoInput(text="a"))
    assert base != idempotency_key("E1", "r1", "t", EchoInput(text="b"))


def test_duplicate_tool_names_are_rejected() -> None:
    with pytest.raises(ValueError, match="Duplicate"):
        ToolExecutor([_spec(_echo), _spec(_echo)])
