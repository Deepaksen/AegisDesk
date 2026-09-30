"""AegisDesk Streamlit UI (spec section 34, Milestone 10).

    uv run aegisdesk api serve                      # the API, http://127.0.0.1:8000
    uv run streamlit run apps/ui/streamlit_app.py   # this UI, http://localhost:8501

The UI talks to the API over HTTP only (`aegisdesk.ui.client`). It never sees
chain-of-thought: it shows the answer, safe activity summaries, citations,
references and pending approvals that the API returns.

Signing in here only picks the synthetic employee whose ID the UI sends in the
`X-Employee-Id` header, standing in for the authenticating gateway (ADR 0015).
"""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import quote

import streamlit as st

from aegisdesk.ui.client import ApiClient, ApiError

DEMO_USERS = {
    "E1004": "Aisha Khan (finance employee)",
    "E1001": "Priya Raman (engineering)",
    "E1005": "Tom Becker (contractor)",
    "E1010": "Grace Liu (finance manager)",
    "E1013": "Kenji Watanabe (engineering manager, IT admin)",
    "E1015": "Ines Duarte (security approver)",
    "E1016": "Viktor Lindqvist (data owner)",
    "E1006": "Lena Hoffmann (IT admin)",
}
STATUS_ICONS = {"ok": "✅", "error": "⚠️", "waiting": "⏸️"}

ClientFactory = Callable[[str], Any]


def _client(employee_id: str) -> Any:
    factory: ClientFactory | None = st.session_state.get("client_factory")
    if factory is not None:  # tests inject a stub
        return factory(employee_id)
    return ApiClient.connect(st.session_state.api_url, employee_id)


def _trace_link(trace_id: str | None) -> str:
    if not trace_id:
        return ""
    grafana = os.environ.get("GRAFANA_URL")
    if not grafana:
        return f"trace `{trace_id}`"
    # Grafana Explore on the Tempo datasource (provisioned in M8), showing this trace.
    left = {"datasource": "tempo", "queries": [{"query": trace_id, "queryType": "traceql"}]}
    return f"[trace {trace_id[:12]}...]({grafana}/explore?left={quote(json.dumps(left))})"


def _render_result(result: dict[str, Any]) -> None:
    st.markdown(result["answer"])
    if result.get("citations"):
        st.caption(
            "Sources: " + "; ".join(f"{c['document_id']} {c['title']}" for c in result["citations"])
        )
    if result.get("references"):
        st.caption("References: " + " · ".join(f"`{r}`" for r in result["references"]))
    for pending in result.get("pending_approvals", []):
        st.info(
            f"⏸️ Waiting for {pending['step'].replace('_', ' ')} approval "
            f"({pending['approval_id']}, approver {pending['approver']}) for "
            f"{pending['access_request_id']}. This conversation resumes when it is decided."
        )
    meta = _trace_link(result.get("trace_id"))
    st.caption(f"request `{result['request_id']}` · {meta}")


def _sidebar() -> str:
    st.sidebar.title("AegisDesk")
    st.session_state.setdefault("api_url", os.environ.get("AEGIS_API_URL", "http://127.0.0.1:8000"))
    st.sidebar.text_input("API URL", key="api_url")
    employee_id = st.sidebar.selectbox(
        "Signed in as (synthetic)",
        list(DEMO_USERS),
        format_func=lambda e: f"{e} - {DEMO_USERS[e]}",
        key="employee_id",
    )
    if st.sidebar.button("New conversation", key="new_thread"):
        st.session_state.setdefault("threads", {}).pop(employee_id, None)
        st.session_state.setdefault("turns", {}).pop(employee_id, None)
    st.sidebar.caption(
        "Development login: the UI sends X-Employee-Id as an authenticating gateway would."
    )
    return str(employee_id)


def _assistant(api: Any, employee_id: str) -> None:
    threads: dict[str, str] = st.session_state.setdefault("threads", {})
    turns: dict[str, list[dict[str, Any]]] = st.session_state.setdefault("turns", {})
    history = turns.setdefault(employee_id, [])

    # Messages go in a container above the input, so a new turn renders in order.
    conversation = st.container()
    text = st.chat_input("Ask the service desk...")
    with conversation:
        for turn in history:
            with st.chat_message(turn["role"]):
                if turn["role"] == "user":
                    st.markdown(turn["text"])
                else:
                    _render_result(turn["result"])
        if text:
            _new_turn(api, employee_id, text, threads, history)


def _new_turn(
    api: Any,
    employee_id: str,
    text: str,
    threads: dict[str, str],
    history: list[dict[str, Any]],
) -> None:
    with st.chat_message("user"):
        st.markdown(text)
    history.append({"role": "user", "text": text})
    try:
        if employee_id not in threads:
            threads[employee_id] = api.new_thread()
        result: dict[str, Any] | None = None
        with st.chat_message("assistant"):
            with st.status("Working on it...", expanded=True) as status:
                # One key per submission: a retry of this message never writes twice.
                for event in api.stream(threads[employee_id], text, str(uuid.uuid4())):
                    if event.event == "activity":
                        icon = STATUS_ICONS.get(event.data["status"], "•")
                        st.write(f"{icon} {event.data['text']}")
                    elif event.event == "result":
                        result = event.data
                    elif event.event == "error":
                        raise ApiError(event.data["status"], event.data["title"])
                status.update(label="Done", state="complete", expanded=False)
            if result is not None:
                _render_result(result)
                history.append({"role": "assistant", "result": result})
    except ApiError as exc:
        st.error(f"The request failed: {exc}")


def _approvals(api: Any) -> None:
    try:
        pending = api.approvals()
    except ApiError as exc:
        st.error(f"Could not load approvals: {exc}")
        return
    last = st.session_state.pop("last_decision", None)
    if last:
        st.success(last["message"])
        if last.get("resumed"):
            with st.chat_message("assistant"):
                _render_result(last["resumed"])
    if not pending:
        st.write("No approvals are waiting for you.")
        return
    for a in pending:
        with st.container(border=True):
            st.markdown(
                f"**{a['approval_id']}** · {a['application_name']} for "
                f"{a['requester_name']} ({a['requester_id']}) · step: {a['step']} · "
                f"expires {a['expires_at'][:16].replace('T', ' ')}"
            )
            comment = st.text_input("Comment (optional)", key=f"comment-{a['approval_id']}")
            left, right = st.columns(2)
            choice = None
            if left.button("Approve", key=f"approve-{a['approval_id']}", type="primary"):
                choice = True
            if right.button("Reject", key=f"reject-{a['approval_id']}"):
                choice = False
            if choice is not None:
                try:
                    decision = api.decide(a["approval_id"], approve=choice, comment=comment)
                except ApiError as exc:
                    st.error(f"Refused: {exc}")
                    continue
                verb = "Approved" if choice else "Rejected"
                note = f" {decision['note']}" if decision.get("note") else ""
                st.session_state.last_decision = {
                    "message": f"{verb} {a['approval_id']}; request is now "
                    f"{decision['request_status']}.{note}",
                    "resumed": decision.get("resumed"),
                }
                st.rerun()


def _audit(api: Any) -> None:
    request_id = st.text_input("Filter by request ID (optional)", key="audit_request_id")
    try:
        events = api.audit(request_id or None)
    except ApiError as exc:
        st.error(f"Could not load audit events: {exc}")
        return
    if not events:
        st.write("No audit events visible to you yet.")
        return
    columns = (
        "occurred_at",
        "phase",
        "tool",
        "policy_decision",
        "outcome",
        "approval_id",
        "approver_id",
        "request_id",
        "trace_id",
    )
    st.dataframe([{k: e.get(k) for k in columns} for e in events], width="stretch")


def main() -> None:
    st.set_page_config(page_title="AegisDesk", page_icon="🛡️", layout="wide")
    employee_id = _sidebar()
    api = _client(employee_id)
    try:
        me = api.me()
    except ApiError as exc:
        st.error(f"Cannot sign in as {employee_id}: {exc}")
        return
    st.header(f"Hello, {me['name']}")
    st.caption(f"{me['department']} · roles: {', '.join(me['roles'])}")
    assistant, approvals, audit = st.tabs(["Service desk", "Approvals", "Audit trail"])
    with assistant:
        _assistant(api, employee_id)
    with approvals:
        _approvals(api)
    with audit:
        _audit(api)


main()
