"""The Service Desk graph's state.

In M1 the agent's memory was a local `messages` list inside `run()`. In
LangGraph, everything a run knows lives in one typed **state** object:

* every node receives the current state and returns a *partial update*;
* each key has a **reducer** that decides how an update is merged. The
  default is "replace"; `messages` uses `add_messages`, which appends (and
  replaces a message whose ID matches);
* after every node, the **checkpointer** saves the whole state for the
  thread. That is what lets a conversation survive a process restart.

Because state is persisted, it must be serialisable and must never contain
secrets. The user's claims are stored so tools know who is acting; they come
from the authenticated caller on every turn, never from the model.
"""

from __future__ import annotations

from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages

from aegisdesk.identity.context import UserContext


class UserClaims(TypedDict):
    employee_id: str
    roles: list[str]
    department: str
    manager_id: str | None


class ServiceDeskState(TypedDict, total=False):
    # Conversation across all turns of the thread. The system prompt is NOT stored here.
    messages: Annotated[list[BaseMessage], add_messages]

    # Who the thread belongs to (set on the first turn) and who is acting now.
    owner_id: str
    user: UserClaims

    # Per-turn bookkeeping, reset by `start_turn`.
    request_id: str
    llm_calls: int
    tool_calls: int
    input_tokens: int
    output_tokens: int
    stop_reason: str | None
    trajectory: list[dict[str, Any]]


def claims_from(user: UserContext) -> UserClaims:
    return UserClaims(
        employee_id=user.employee_id,
        roles=list(user.roles),
        department=user.department,
        manager_id=user.manager_id,
    )


def context_from(claims: UserClaims) -> UserContext:
    return UserContext(
        employee_id=claims["employee_id"],
        roles=tuple(claims["roles"]),
        department=claims["department"],
        manager_id=claims["manager_id"],
    )
