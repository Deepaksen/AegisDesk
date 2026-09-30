"""The local observability stack's configuration (docker compose profile `observability`).

Docker is not needed: the files are parsed and cross-checked against the
metrics the application actually emits.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import yaml

from aegisdesk.config import PROJECT_ROOT
from aegisdesk.observability.metrics import prometheus_names

INFRA = PROJECT_ROOT / "infra" / "observability"
DASHBOARD = INFRA / "grafana" / "dashboards" / "aegisdesk-overview.json"


def _yaml(path: Path) -> dict:  # type: ignore[type-arg]
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), path
    return data


def test_all_config_files_parse() -> None:
    for path in INFRA.rglob("*.y*ml"):
        _yaml(path)
    json.loads(DASHBOARD.read_text(encoding="utf-8"))


def test_collector_routes_traces_to_tempo_and_metrics_to_prometheus() -> None:
    config = _yaml(INFRA / "otel-collector.yaml")
    pipelines = config["service"]["pipelines"]

    assert pipelines["traces"]["exporters"] == ["otlp/tempo"]
    assert pipelines["metrics"]["exporters"] == ["prometheus"]
    assert "attributes/redact" in pipelines["traces"]["processors"]  # second redaction layer


def test_dashboard_only_queries_metrics_the_app_emits() -> None:
    dashboard = json.loads(DASHBOARD.read_text(encoding="utf-8"))
    exprs = [t["expr"] for p in dashboard["panels"] for t in p["targets"]]
    used = {m for e in exprs for m in re.findall(r"aegisdesk_[a-z_]+", e)}

    assert used and used <= prometheus_names(), used - prometheus_names()
    titles = {p["title"] for p in dashboard["panels"]}
    for required in (
        "Request volume",
        "Success rate",
        "Task latency p50 / p95",
        "LLM calls per request",
        "Tool calls per request",
        "Token usage by model",
        "Policy denials by reason",
        "Approval rate",
        "Top failing tools",
    ):
        assert required in titles  # spec section 24


def test_dashboard_file_is_generated_from_the_script() -> None:
    spec = importlib.util.spec_from_file_location(
        "build_dashboard", PROJECT_ROOT / "scripts" / "build_dashboard.py"
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert json.loads(DASHBOARD.read_text(encoding="utf-8")) == module.build()


def test_compose_mounts_existing_files() -> None:
    compose = _yaml(PROJECT_ROOT / "docker-compose.yml")
    services = compose["services"]
    for name in ("otel-collector", "tempo", "prometheus", "grafana"):
        assert services[name]["profiles"] == ["observability"]
        for volume in services[name].get("volumes", []):
            source = volume.split(":")[0]
            if source.startswith("./"):
                assert (PROJECT_ROOT / source).exists(), source
