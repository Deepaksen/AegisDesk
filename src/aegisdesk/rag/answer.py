"""Grounded answering: retrieve → construct context → generate → verify citations.

This is plain RAG, with no agent involved: one retrieval and at most one
model call. The deterministic steps around the model are what make the
answer trustworthy:

1. **No evidence, no model call.** If nothing clears the evidence threshold,
   return a fixed "I don't have documentation for that" answer. The model
   never gets the chance to invent a policy.
2. **Context construction.** Each chunk is wrapped in a
   `<document chunk_id=…>` element with its metadata. Chunk text is treated
   as untrusted data: tag-like sequences that could close the element early
   are neutralised.
3. **Structured output.** The model returns `answer`, `citations` (chunk IDs)
   and `insufficient_evidence`.
4. **Citation verification.** Every cited chunk ID must be one that was
   actually retrieved for this user. An answer with no citations, or with an
   invented one, is discarded and replaced by a safe fallback.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Annotated

from pydantic import BaseModel, Field

from aegisdesk.identity.context import UserContext
from aegisdesk.llm.client import CallMetadata, LLMClient, StructuredOutputError
from aegisdesk.prompts.loader import Prompt
from aegisdesk.rag.models import ScoredChunk
from aegisdesk.rag.retrieval.retriever import RetrievalResult, Retriever

CHUNK_ID_PATTERN = r"^DOC-[A-Z]+-\d{3}#\d{2}$"

NO_EVIDENCE_ANSWER = (
    "I couldn't find anything in the Northstar documentation available to you that answers "
    "this. Please contact the IT service desk (extension 4357) or rephrase your question."
)
UNGROUNDED_ANSWER = (
    "I couldn't produce an answer that is properly supported by the documentation. "
    "Please contact the IT service desk (extension 4357)."
)


class GroundedAnswer(BaseModel):
    """Answer to an employee's question, based only on the provided documents."""

    answer: str = Field(
        description="The answer in plain language, based only on the documents.", max_length=2000
    )
    citations: list[Annotated[str, Field(pattern=CHUNK_ID_PATTERN)]] = Field(
        description="chunk_id of every document chunk the answer relies on."
    )
    insufficient_evidence: bool = Field(
        description="True if the documents do not contain the answer."
    )


class AnswerStatus(StrEnum):
    ANSWERED = "answered"
    NO_EVIDENCE = "no_evidence"  # retrieval found nothing; model not called
    MODEL_DECLINED = "model_declined"  # model said the documents don't answer it
    UNGROUNDED = "ungrounded"  # missing, invalid or invented citations


@dataclass(frozen=True)
class Citation:
    chunk_id: str
    document_id: str
    title: str
    version: str
    section: str


@dataclass(frozen=True)
class GroundedAnswerResult:
    question: str
    answer: str
    status: AnswerStatus
    citations: list[Citation]
    retrieval: RetrievalResult
    llm: CallMetadata | None
    rejected_citations: list[str]


def _neutralise(text: str) -> str:
    # Chunk text must not be able to close or open <document> elements.
    return re.sub(r"</?\s*documents?\b", lambda m: html.escape(m.group(0)), text, flags=re.I)


def build_context(chunks: list[ScoredChunk]) -> str:
    parts = []
    for scored in chunks:
        chunk, meta = scored.chunk, scored.chunk.metadata
        parts.append(
            f'<document chunk_id="{chunk.chunk_id}" title="{html.escape(meta.title)}" '
            f'version="{meta.version}" effective_date="{meta.effective_date}" '
            f'section="{html.escape(chunk.section)}">\n{_neutralise(chunk.text)}\n</document>'
        )
    return "<documents>\n" + "\n".join(parts) + "\n</documents>"


class GroundedAnswerer:
    def __init__(self, retriever: Retriever, client: LLMClient, prompt: Prompt) -> None:
        self._retriever = retriever
        self._client = client
        self._prompt = prompt

    def answer(self, question: str, user: UserContext) -> GroundedAnswerResult:
        retrieval = self._retriever.retrieve(question, user)
        if not retrieval.sufficient_evidence:
            return GroundedAnswerResult(
                question, NO_EVIDENCE_ANSWER, AnswerStatus.NO_EVIDENCE, [], retrieval, None, []
            )

        user_message = f"Question: {question}\n\n{build_context(retrieval.chunks)}"
        try:
            response = self._client.structured(self._prompt, user_message, GroundedAnswer)
        except StructuredOutputError:
            return GroundedAnswerResult(
                question, UNGROUNDED_ANSWER, AnswerStatus.UNGROUNDED, [], retrieval, None, []
            )
        output, meta = response.value, response.metadata

        if output.insufficient_evidence:
            return GroundedAnswerResult(
                question, NO_EVIDENCE_ANSWER, AnswerStatus.MODEL_DECLINED, [], retrieval, meta, []
            )

        retrieved = {s.chunk.chunk_id: s.chunk for s in retrieval.chunks}
        cited = list(dict.fromkeys(output.citations))  # de-duplicate, keep order
        rejected = [c for c in cited if c not in retrieved]
        if not cited or rejected:
            return GroundedAnswerResult(
                question, UNGROUNDED_ANSWER, AnswerStatus.UNGROUNDED, [], retrieval, meta, rejected
            )

        citations = [
            Citation(
                chunk_id=c,
                document_id=retrieved[c].document_id,
                title=retrieved[c].metadata.title,
                version=retrieved[c].metadata.version,
                section=retrieved[c].section,
            )
            for c in cited
        ]
        return GroundedAnswerResult(
            question, output.answer, AnswerStatus.ANSWERED, citations, retrieval, meta, []
        )
