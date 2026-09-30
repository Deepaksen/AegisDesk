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
