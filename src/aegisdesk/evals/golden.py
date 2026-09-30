"""The golden dataset: what a case is and what it expects (spec sections 26-27).

A case describes one user request and the *behaviour* the system must show,
not the wording of the answer:

    expected.agents          which specialists handle it (routing)
    expected.required_tools  tools that must be called
    expected.forbidden_tools tools that must not even be requested
    expected.tool_args       argument values that matter (subset match)
    expected.policy          policy decision per tool (from audit events)
    expected.requires_approval
    expected.expected_facts  substrings the answer must contain
    expected.forbidden_facts substrings it must not (leaks)
    expected.expected_citations  document IDs the answer must cite
    expected.effects         the only writes allowed (anything more is an
                             unauthorized action)
    limits                   max model and tool calls

`setup` makes behaviours reproducible offline: a scripted model (adversarial
trajectories "regardless of what the model outputs"), injected faults,
repeated submission, the MCP transport, and human approval decisions.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Category(StrEnum):
    KNOWLEDGE = "knowledge"
    SERVICE_DESK = "service_desk"
    ACCESS = "access"
    MULTI_INTENT = "multi_intent"
    SECURITY = "security"
    FAILURE = "failure"


class CaseUser(_Strict):
    employee_id: str
    roles: list[str] = []  # documentation; checked against the seed data by a test


class ToolCall(_Strict):
    name: str
    args: dict[str, Any] = {}


class Route(_Strict):
    agent: str
    instruction: str = "handle the request"


class ScriptStep(_Strict):
    """One scripted model reply: a routing plan, tool calls, or a text answer."""

    route: list[Route] | None = None
    tool_calls: list[ToolCall] | None = None
    text: str | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> ScriptStep:
        if sum(x is not None for x in (self.route, self.tool_calls, self.text)) != 1:
            raise ValueError("a script step is exactly one of route, tool_calls, text")
        return self


class Decision(_Strict):
    approver: str
    decision: Literal["approve", "reject"] = "approve"
    comment: str | None = None
    step: str | None = None  # manager | security | data_owner; first pending if omitted
    # The decision must be refused with this category (e.g. not_found for the wrong manager).
    expect_refusal: str | None = None


class Setup(_Strict):
    transport: Literal["local", "mcp_inprocess"] = "local"
    faults: str | None = None  # AEGIS_FAULTS syntax
    script: list[ScriptStep] | None = None
    repeat: int = Field(default=1, ge=1, le=3)  # same request ID each time
    decisions: list[Decision] = []
    # System configurations this case applies to (e.g. scripted multi-agent trajectories).
    configs: list[str] | None = None


class Effects(_Strict):
    tickets_created: int = 0
    comments_added: int = 0
    access_requests_created: int = 0
    access_granted: list[str] = []  # application IDs


class Expected(_Strict):
    agents: list[str] | None = None
    out_of_scope: bool | None = None
    required_tools: list[str] = []
    forbidden_tools: list[str] = []
    tool_args: dict[str, dict[str, Any]] = {}
    policy: dict[str, Literal["allow", "deny", "require_approval"]] = {}
    requires_approval: bool | None = None
    expected_facts: list[str] = []
    # Must NOT appear in the answer: another employee's data, system prompt text.
    forbidden_facts: list[str] = []
    expected_citations: list[str] = []
    effects: Effects = Effects()


class Limits(_Strict):
    max_llm_calls: int | None = None
    max_tool_calls: int | None = None


class GoldenCase(_Strict):
    id: str
    category: Category
    description: str
    user: CaseUser
    input: str
    # Repeat the input N times (oversized-input cases without pasting kilobytes into YAML).
    input_repeat: int = Field(default=1, ge=1, le=1000)
    setup: Setup = Setup()
    expected: Expected = Expected()
    limits: Limits = Limits()
    # A string planted in the input that must never appear in telemetry or audit.
    secret_marker: str | None = None

    @property
    def text(self) -> str:
        return self.input * self.input_repeat


class GoldenDataset(_Strict):
    name: str
    version: int
    today: date  # eligibility depends on dates; pinned for reproducibility
    cases: list[GoldenCase]

    @model_validator(mode="after")
    def _unique_ids(self) -> GoldenDataset:
        ids = [c.id for c in self.cases]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ValueError(f"duplicate case ids: {dupes}")
        return self

    @classmethod
    def load(cls, path: Path) -> GoldenDataset:
        return cls.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
