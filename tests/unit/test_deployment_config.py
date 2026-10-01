"""Milestone 10: the container and compose definitions, checked without Docker.

`docker compose up` (spec section 43) starts postgres, migrate, mcp, api and ui;
these tests keep the files consistent with the code: commands exist in the CLI,
files referenced exist, secrets are not hard-coded, and the startup order holds.
"""

from __future__ import annotations

import shlex
from typing import Any

import pytest
import yaml

from aegisdesk.cli import build_parser
from aegisdesk.config import PROJECT_ROOT

COMPOSE = PROJECT_ROOT / "docker-compose.yml"
DOCKERFILE = PROJECT_ROOT / "Dockerfile"


@pytest.fixture(scope="module")
def services() -> dict[str, Any]:
    data = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return dict(data["services"])


def test_default_stack_has_the_platform_services(services: dict[str, Any]) -> None:
    default = {name for name, s in services.items() if "profiles" not in s}
    assert default == {"postgres", "migrate", "mcp", "api", "ui"}


@pytest.mark.parametrize("name", ["migrate", "mcp", "api"])
def test_app_commands_are_real_cli_commands(services: dict[str, Any], name: str) -> None:
    command = services[name]["command"]
    argv = shlex.split(command[-1]) if command[0] == "sh" else command
    for part in " ".join(argv).split("&&"):
        words = part.split()
        assert words[0] == "aegisdesk"
        build_parser().parse_args(words[1:])  # SystemExit if the CLI has no such command


def test_ui_runs_the_streamlit_app_that_exists(services: dict[str, Any]) -> None:
    command = services["ui"]["command"]
    assert command[:2] == ["streamlit", "run"]
    assert (PROJECT_ROOT / command[2]).is_file()
    assert services["ui"]["environment"]["AEGIS_API_URL"] == "http://api:8000"
    assert services["ui"]["build"]["args"]["GROUPS"] == "--group ui"


def test_startup_order_and_health(services: dict[str, Any]) -> None:
    assert services["migrate"]["depends_on"]["postgres"]["condition"] == "service_healthy"
    assert services["mcp"]["depends_on"]["migrate"]["condition"] == (
        "service_completed_successfully"
    )
    assert services["api"]["depends_on"]["mcp"]["condition"] == "service_healthy"
    assert "/ready" in " ".join(services["api"]["healthcheck"]["test"])


def test_api_uses_shared_stores_and_remote_tools(services: dict[str, Any]) -> None:
    env = services["api"]["environment"]
    for key in ("DATA_STORE", "AUDIT_STORE", "CHECKPOINT_STORE"):
        assert env[key] == "postgres"
    assert env["TOOL_TRANSPORT"] == "mcp_http"
    assert env["MCP_ACTION_URL"].startswith("http://mcp:8765/")
    # The MCP servers share the same stores: approvals written by the API are visible there.
    assert services["mcp"]["environment"]["DATA_STORE"] == "postgres"


def test_secrets_come_from_the_environment(services: dict[str, Any]) -> None:
    secret = services["api"]["environment"]["MCP_TOKEN_SECRET"]
    assert secret == "${MCP_TOKEN_SECRET:-}"  # never a literal; empty means "not configured"
    assert "ports" not in services["mcp"]  # internal network only


def test_dockerfile_installs_the_locked_dependencies_as_non_root() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "uv sync --locked" in text and "--no-default-groups" in text
    assert "USER aegis" in text
    for copied in ("src", "apps", "config", "prompts", "data", "migrations"):
        assert f"COPY {copied} " in text and (PROJECT_ROOT / copied).exists()
    ignored = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").split()
    assert ".env" in ignored and ".venv" in ignored
