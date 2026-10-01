"""Deterministic citation check for the Knowledge agent.

The Knowledge agent must ground every answer in passages its own searches
returned. The model is asked to cite `[DOC-XXX-NNN#NN]`. This check makes
that a rule instead of a request, the same rule `rag/answer.py` applies to
plain RAG:

* every chunk ID mentioned in the answer must have been returned by one of
  this run's `search_knowledge_base` / `retrieve_document` calls;
* if the searches did find evidence, the answer must cite at least one chunk.

Otherwise the answer is replaced by a safe fallback.
"""

from __future__ import annotations

import re
from typing import Any

from aegisdesk.rag.answer import UNGROUNDED_ANSWER

_CHUNK_ID = re.compile(r"DOC-[A-Z]+-\d{3}#\d{2}")
_EVIDENCE_TOOLS = frozenset({"search_knowledge_base", "retrieve_document"})


def check_knowledge_answer(answer: str, trajectory: list[dict[str, Any]]) -> tuple[str, str | None]:
    retrieved: set[str] = set()
    for entry in trajectory:
        is_evidence = entry.get("kind") == "tool" and entry.get("tool_name") in _EVIDENCE_TOOLS
        if is_evidence and entry.get("status") == "ok":
            retrieved.update(_CHUNK_ID.findall(entry.get("result", "")))

    cited = set(_CHUNK_ID.findall(answer))
    invented = sorted(cited - retrieved)
    if invented:
        return UNGROUNDED_ANSWER, f"citations not retrieved in this run: {invented}"
    if retrieved and not cited:
        return UNGROUNDED_ANSWER, "answer did not cite the retrieved documentation"
    return answer, None
