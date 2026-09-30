"""Deliberate failure injection, for learning to debug with telemetry (spec, Milestone 8).

`AEGIS_FAULTS` is a comma-separated list of `kind` or `kind:target`:

* `tool_error:<tool>`        the tool handler raises (-> internal_error)
* `tool_timeout:<tool>`      a remote tool call times out (-> timeout)
* `mcp_unavailable:<server>` the MCP server is unreachable (-> unavailable)
* `retrieval_error`          the knowledge base search fails

Milestone 11 (reliability) adds the spec's failure list:

* `model_timeout[:target]`     every model call attempt times out (`target`: `router`,
                               or an agent's prompt name such as `service_desk`)
* `model_unavailable[:target]` the model service is unreachable
* `model_malformed[:target]`   the model returns broken output (unparsable routing,
                               or a tool call whose arguments are not JSON)
* `db_error:<store>`           a database is down: `audit`, `access` or `checkpoint`
* duplicate requests need no fault: send the same Idempotency-Key twice

Faults are read once from the environment and are off unless set. They exist
only at seams that already handle the corresponding real failure, so the
code paths exercised are the production ones.
"""

from __future__ import annotations

import os
from functools import lru_cache

ENV = "AEGIS_FAULTS"


class InjectedFaultError(RuntimeError):
    """Raised where a fault is injected; handled like the real failure it simulates."""


@lru_cache(maxsize=1)
def _faults() -> frozenset[tuple[str, str]]:
    raw = os.environ.get(ENV, "")
    parsed = set()
    for item in raw.split(","):
        item = item.strip()
        if item:
            kind, _, target = item.partition(":")
            parsed.add((kind.strip(), target.strip()))
    return frozenset(parsed)


def active(kind: str, target: str = "") -> bool:
    faults = _faults()
    return (kind, target) in faults or (kind, "") in faults


def reload() -> None:
    _faults.cache_clear()
