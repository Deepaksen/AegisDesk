"""The CLI runs end to end on the offline fake model."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aegisdesk.cli import main


def test_triage_prints_structured_result_and_metadata(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["triage", "My VPN drops every 10 minutes"]) == 0

    out = capsys.readouterr().out
    body, meta_line = out.strip().rsplit("\n", 1)
    assert json.loads(body)["category"] == "vpn"
    assert "prompt=triage@v1" in meta_line
    assert "tokens in=" in meta_line


def test_chat_prints_answer(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["chat", "hello"]) == 0
    assert "You said: hello" in capsys.readouterr().out


def test_repeat_reports_distinct_answers(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["repeat", "hello", "--runs", "3", "--temperature", "1.0"]) == 0
    assert "1 distinct answer(s) from 3 run(s) at temperature=1.0" in capsys.readouterr().out


def test_config_never_prints_the_api_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-appear")
    assert main(["config"]) == 0
    out = capsys.readouterr().out
    assert "sk-ant-should-not-appear" not in out
    assert '"anthropic_api_key": "set"' in out


def test_configuration_errors_exit_cleanly(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("MODEL_NAME", "not-allowlisted")
    assert main(["chat", "hi"]) == 2
    assert "not allowlisted" in capsys.readouterr().err


def test_agent_single_request_prints_trajectory_and_answer(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["agent", "--as", "E1004", "What laptop is assigned to me?"]) == 0

    out = capsys.readouterr().out
    assert "→ router" in out and "service_desk: 'What laptop is assigned to me?'" in out
    assert "[service_desk] step 1 tool   get_my_assets({}) ok" in out
    assert "← service_desk done" in out
    assert "Assistant: [fake model] Tool results:" in out
    assert "NS-LT-0101" in out
    # 1 routing call + 2 specialist calls
    assert "supervisor@0.1.0 prompt=router@v1" in out
    assert "stop=final_answer llm_calls=3 tool_calls=1" in out


def test_single_agent_graph_engine_is_still_available(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["agent", "--as", "E1004", "--engine", "graph", "What laptop is assigned to me?"]) == 0
    )
    out = capsys.readouterr().out
    assert "  · step 1 tool   get_my_assets({}) ok" in out
    assert "stop=final_answer llm_calls=2 tool_calls=1" in out


def test_agent_refuses_terminated_employee(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["agent", "--as", "E1007", "hi"]) == 2
    assert "not active" in capsys.readouterr().err


def test_agent_interactive_session_keeps_state_between_turns(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    turns = iter(["Create a ticket: my VPN keeps dropping", "What tickets do I have open?", "exit"])
    monkeypatch.setattr("builtins.input", lambda _prompt: next(turns))

    assert main(["agent", "--as", "E1004", "--quiet"]) == 0

    out = capsys.readouterr().out
    # The ticket created in turn 1 is listed in turn 2.
    assert out.count("INC-1008") >= 2


def test_agent_thread_resumes_across_invocations(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["agent", "--as", "E1004", "--thread", "cli-t1", "What laptop is assigned to me?"])
        == 0
    )
    assert main(["agent", "--as", "E1004", "--thread", "cli-t1", "Show me ticket INC-1001"]) == 0
    capsys.readouterr()

    assert main(["thread", "cli-t1", "--as", "E1004"]) == 0
    out = capsys.readouterr().out
    assert "human: What laptop is assigned to me?" in out
    assert "human: Show me ticket INC-1001" in out
    assert "INC-1001" in out
    # Context isolation: the thread keeps visible turns only, not specialists' tool traffic.
    assert "tool:" not in out


def test_thread_of_another_employee_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["agent", "--as", "E1004", "--thread", "cli-t2", "hello"]) == 0
    capsys.readouterr()

    assert main(["agent", "--as", "E1001", "--thread", "cli-t2", "hello"]) == 2
    assert main(["thread", "cli-t2", "--as", "E1001"]) == 2
    assert "belongs to another employee" in capsys.readouterr().err


def test_loop_engine_is_still_available(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["agent", "--as", "E1004", "--engine", "loop", "What laptop is assigned to me?"]) == 0
    )
    out = capsys.readouterr().out
    assert "tool   get_my_assets({}) ok" in out
    assert "thread_id=" not in out


def test_rag_search_shows_scored_chunks(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["rag", "search", "error GP-512", "--as", "E1004", "--k", "2"]) == 0
    out = capsys.readouterr().out
    assert "DOC-VPN-001#05 | VPN Troubleshooting Guide v2.4 | Error GP-512 | internal" in out


def test_ask_cites_sources_and_declines_without_evidence(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["ask", "What does error GP-512 mean?", "--as", "E1004"]) == 0
    out = capsys.readouterr().out
    assert "[DOC-VPN-001#05] VPN Troubleshooting Guide v2.4" in out
    assert "status=answered" in out

    assert main(["ask", "What is on the canteen menu?", "--as", "E1004"]) == 0
    out = capsys.readouterr().out
    assert "status=no_evidence" in out and "[model not called]" in out


def test_eval_rag_passes_gate(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["eval", "rag", "--min-hit-rate", "0.85"]) == 0
    assert "access violations     0" in capsys.readouterr().out
    assert main(["eval", "rag", "--min-hit-rate", "0.99"]) == 1


def test_agent_answers_policy_questions_from_the_knowledge_base(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert main(["agent", "--as", "E1004", "What does VPN error GP-512 mean?"]) == 0
    out = capsys.readouterr().out
    assert "search_knowledge_base" in out
    assert "DOC-VPN-001#05" in out


def test_policy_check_explains_a_denial(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["policy", "check", "--as", "E1004", "--agent", "knowledge", "--tool", "create_ticket"]
    )

    out = capsys.readouterr().out
    assert code == 1
    assert out.startswith("DENY") and "reason: agent_not_authorized_for_tool" in out


def test_policy_check_allows_an_authorized_call(capsys: pytest.CaptureFixture[str]) -> None:
    code = main(
        ["policy", "check", "--as", "E1004", "--agent", "access", "--tool", "create_access_request"]
    )

    assert code == 0 and capsys.readouterr().out.startswith("ALLOW")


def test_broken_policy_file_is_a_configuration_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("POLICY_PATH", str(tmp_path / "missing.yaml"))

    assert main(["agent", "--as", "E1004", "--quiet", "What laptop do I have?"]) == 2
    assert "cannot load policy" in capsys.readouterr().err


def test_audit_with_memory_store_explains_itself(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["audit", "--user", "E1004"]) == 0
    assert "memory store is per process" in capsys.readouterr().out


def test_approvals_list_explains_the_memory_store(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["approvals", "list", "--as", "E1010"]) == 0
    out = capsys.readouterr().out
    assert "No approvals waiting for E1010" in out and "DATA_STORE=postgres" in out


def test_approving_an_unknown_approval_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["approvals", "approve", "AP-9999", "--as", "E1010"]) == 2
    assert "Refused (not_found)" in capsys.readouterr().err


def test_agent_shows_the_pause(capsys: pytest.CaptureFixture[str]) -> None:
    message = "Please create an access request for FinanceERP for month-end reporting"
    assert main(["agent", "--as", "E1004", "--quiet", message]) == 0
    out = capsys.readouterr().out
    assert "⏸ Waiting for approval: AP-0001 (manager: E1010)" in out


def test_agent_trace_prints_the_span_tree(capsys: pytest.CaptureFixture[str]) -> None:
    assert (
        main(["agent", "--as", "E1004", "--quiet", "--trace", "What laptop is assigned to me?"])
        == 0
    )
    out = capsys.readouterr().out
    assert "aegisdesk.request" in out and "execute_tool get_my_assets" in out


def test_telemetry_check_prints_no_secrets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("LANGSMITH_API_KEY", "lsv2_should_not_appear")
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "authorization=Bearer should-not-appear")

    assert main(["telemetry"]) == 0
    out = capsys.readouterr().out
    assert "should-not-appear" not in out and "lsv2_should_not_appear" not in out
    report = json.loads(out)
    assert report["exporter"] == "none" and report["otlp_headers"] == "set"


def test_reconcile_needs_an_it_admin(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["approvals", "reconcile", "--as", "E1004"]) == 2
    assert "it_admin" in capsys.readouterr().err
    assert main(["approvals", "reconcile", "--as", "E1006"]) == 0
    assert "Nothing to reconcile" in capsys.readouterr().out
