"""HTTP client for the AegisDesk API, used by the Streamlit UI (Milestone 10).

The UI is just another API client: it imports this module and nothing else from
the backend, so the backend is never shaped around the UI. Only `httpx`.
"""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Protocol

import httpx

EMPLOYEE_HEADER = "X-Employee-Id"


class HttpClient(Protocol):
    """What the client needs from an HTTP library (httpx, or a test client)."""

    def request(
        self,
        method: str,
        url: str,
        *,
        json: Any = ...,
        params: Mapping[str, str] | None = ...,
        headers: Mapping[str, str] | None = ...,
    ) -> Any: ...

    def stream(
        self, method: str, url: str, *, json: Any = ..., headers: Mapping[str, str] | None = ...
    ) -> AbstractContextManager[Any]: ...


class ApiError(Exception):
    """A problem+json response, or a transport failure, in a form the UI can show."""

    def __init__(self, status: int, title: str, detail: str | None = None) -> None:
        super().__init__(f"{status} {title}" + (f": {detail}" if detail else ""))
        self.status = status
        self.title = title
        self.detail = detail


@dataclass(frozen=True)
class StreamEvent:
    event: str  # activity | result | error
    data: dict[str, Any]


def parse_sse(lines: Iterator[str]) -> Iterator[StreamEvent]:
    """Server-sent events: `event:` and `data:` lines, a blank line ends each event."""
    event, data = "message", list[str]()
    for line in lines:
        if not line:
            if data:
                yield StreamEvent(event, json.loads("\n".join(data)))
            event, data = "message", []
        elif line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].strip())
    if data:
        yield StreamEvent(event, json.loads("\n".join(data)))


class ApiClient:
    def __init__(self, http: HttpClient, employee_id: str) -> None:
        self._http = http
        self._headers = {EMPLOYEE_HEADER: employee_id}

    @classmethod
    def connect(cls, base_url: str, employee_id: str, timeout: float = 120.0) -> ApiClient:
        return cls(httpx.Client(base_url=base_url, timeout=timeout), employee_id)

    def _call(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = self._http.request(method, path, headers=self._headers, **kwargs)
        except httpx.HTTPError as exc:
            raise ApiError(503, "API unreachable", type(exc).__name__) from exc
        if response.status_code >= 400:
            try:
                problem = response.json()
            except ValueError:
                problem = {}
            raise ApiError(
                response.status_code,
                problem.get("title", response.reason_phrase),
                problem.get("detail") or problem.get("category"),
            )
        return response.json()

    def me(self) -> dict[str, Any]:
        result: dict[str, Any] = self._call("GET", "/api/v1/me")
        return result

    def new_thread(self) -> str:
        return str(self._call("POST", "/api/v1/threads")["thread_id"])

    def send(self, thread_id: str, text: str, idempotency_key: str | None = None) -> Any:
        headers = {"Idempotency-Key": idempotency_key} if idempotency_key else {}
        return self._call(
            "POST", f"/api/v1/threads/{thread_id}/messages", json={"text": text}, headers=headers
        )

    def stream(
        self, thread_id: str, text: str, idempotency_key: str | None = None
    ) -> Iterator[StreamEvent]:
        headers = {**self._headers, "Accept": "text/event-stream"}
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        try:
            with self._http.stream(
                "POST",
                f"/api/v1/threads/{thread_id}/messages",
                json={"text": text},
                headers=headers,
            ) as response:
                if response.status_code >= 400:
                    response.read()
                    problem = response.json()
                    raise ApiError(
                        response.status_code, problem.get("title", "error"), problem.get("detail")
                    )
                yield from parse_sse(response.iter_lines())
        except httpx.HTTPError as exc:
            raise ApiError(503, "API unreachable", type(exc).__name__) from exc

    def thread(self, thread_id: str) -> Any:
        return self._call("GET", f"/api/v1/threads/{thread_id}")

    def approvals(self) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = self._call("GET", "/api/v1/approvals")
        return result

    def decide(self, approval_id: str, *, approve: bool, comment: str | None) -> Any:
        verb = "approve" if approve else "reject"
        return self._call(
            "POST", f"/api/v1/approvals/{approval_id}/{verb}", json={"comment": comment or None}
        )

    def audit(self, request_id: str | None = None) -> list[dict[str, Any]]:
        params = {"request_id": request_id} if request_id else {}
        result: list[dict[str, Any]] = self._call("GET", "/api/v1/audit", params=params)["events"]
        return result
