"""The CLI runs end to end on the offline fake model."""

from __future__ import annotations

import json

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
    assert "tool   get_my_assets({}) ok" in out
    assert "Assistant: [fake model] Tool results:" in out
    assert "NS-LT-0101" in out
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
    assert "get_ticket" in out


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
