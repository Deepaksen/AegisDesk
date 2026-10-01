"""Generate infra/observability/grafana/dashboards/aegisdesk-overview.json.

The dashboard is code: panels are defined here, and the PromQL only uses
metric names from `aegisdesk.observability.metrics.CATALOGUE` (a test checks
this). Run: uv run python scripts/build_dashboard.py
"""

from __future__ import annotations

import json
from pathlib import Path

OUT = (
    Path(__file__).resolve().parents[1]
    / "infra/observability/grafana/dashboards/aegisdesk-overview.json"
)
W = "[5m]"
REQ = f"sum(rate(aegisdesk_requests_total{W}))"

# (title, [(PromQL, legend)], unit, width)
PANELS: list[tuple[str, list[tuple[str, str]], str, int]] = [
    ("Request volume", [(REQ, "requests/s")], "reqps", 8),
    (
        "Success rate",
        [(f"1 - (sum(rate(aegisdesk_requests_failed_total{W})) or vector(0)) / {REQ}", "success")],
        "percentunit",
        8,
    ),
    (
        "Task latency p50 / p95",
        [
            (
                f"histogram_quantile({q}, sum by (le) "
                f"(rate(aegisdesk_task_latency_seconds_bucket{W})))",
                f"p{int(q * 100)}",
            )
            for q in (0.5, 0.95)
        ],
        "s",
        8,
    ),
    (
        "LLM calls per request",
        [(f"sum(rate(aegisdesk_llm_calls_total{W})) / {REQ}", "llm calls")],
        "short",
        6,
    ),
    (
        "Tool calls per request",
        [(f"sum(rate(aegisdesk_tool_calls_total{W})) / {REQ}", "tool calls")],
        "short",
        6,
    ),
    (
        "Token usage by model",
        [
            (f"sum by (model) (rate(aegisdesk_tokens_input_total{W}))", "in {{model}}"),
            (f"sum by (model) (rate(aegisdesk_tokens_output_total{W}))", "out {{model}}"),
        ],
        "short",
        12,
    ),
    (
        "Policy denials by reason",
        [("sum by (reason) (increase(aegisdesk_policy_denials_total[1h]))", "{{reason}}")],
        "short",
        8,
    ),
    (
        "Approval rate",
        [
            (
                "1 - (sum(increase(aegisdesk_approval_rejections_total[1h])) or vector(0))"
                " / sum(increase(aegisdesk_approval_requests_total[1h]))",
                "approved share",
            )
        ],
        "percentunit",
        8,
    ),
    (
        "Top failing tools",
        [
            (
                "topk(5, sum by (tool, category) (increase(aegisdesk_tool_errors_total[1h])))",
                "{{tool}} {{category}}",
            )
        ],
        "short",
        8,
    ),
    (
        "Tool latency p95 by tool",
        [
            (
                "histogram_quantile(0.95, sum by (le, tool) "
                f"(rate(aegisdesk_tool_latency_seconds_bucket{W})))",
                "{{tool}}",
            )
        ],
        "s",
        12,
    ),
    (
        "LLM latency p95 by model",
        [
            (
                "histogram_quantile(0.95, sum by (le, model) "
                f"(rate(aegisdesk_llm_latency_seconds_bucket{W})))",
                "{{model}}",
            )
        ],
        "s",
        12,
    ),
    (
        "Knowledge base: no-evidence rate and retrieval p95",
        [
            (
                f"sum(rate(aegisdesk_rag_no_evidence_total{W}))"
                f" / sum(rate(aegisdesk_rag_retrieval_latency_seconds_count{W}))",
                "no evidence",
            ),
            (
                "histogram_quantile(0.95, sum by (le) "
                f"(rate(aegisdesk_rag_retrieval_latency_seconds_bucket{W})))",
                "retrieval p95 (s)",
            ),
        ],
        "short",
        12,
    ),
    (
        "Agent invocations",
        [(f"sum by (agent) (rate(aegisdesk_agent_invocations_total{W}))", "{{agent}}")],
        "short",
        12,
    ),
]


def build() -> dict[str, object]:
    panels = []
    x = y = 0
    for i, (title, queries, unit, width) in enumerate(PANELS, start=1):
        if x + width > 24:
            x, y = 0, y + 8
        panels.append(
            {
                "id": i,
                "type": "timeseries",
                "title": title,
                "datasource": {"type": "prometheus", "uid": "prometheus"},
                "gridPos": {"x": x, "y": y, "w": width, "h": 8},
                "fieldConfig": {"defaults": {"unit": unit}, "overrides": []},
                "targets": [
                    {"refId": chr(65 + j), "expr": expr, "legendFormat": legend}
                    for j, (expr, legend) in enumerate(queries)
                ],
            }
        )
        x += width
    return {
        "uid": "aegisdesk-overview",
        "title": "AegisDesk overview",
        "tags": ["aegisdesk"],
        "schemaVersion": 39,
        "time": {"from": "now-1h", "to": "now"},
        "refresh": "30s",
        "panels": panels,
    }


if __name__ == "__main__":
    OUT.write_text(json.dumps(build(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
