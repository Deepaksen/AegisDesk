"""Clean and chunk documents.

Why chunk at all? An embedding is one vector for a whole piece of text. Embed
a whole 2,000-word policy and the vector is a blur of every topic in it; a
question about "error GP-512" matches it only weakly. Small chunks produce
sharp vectors and let us put only the relevant passages into the prompt.
Chunks that are too small lose context ("it expires after 90 days" — what
does?).

Strategy used here (deliberately simple and inspectable):

1. Split on Markdown headings, so a chunk never mixes two sections.
2. Within a section, pack whole paragraphs until about `max_words`.
3. Carry the last paragraph over into the next chunk (overlap) when a
   section needs several chunks, so a fact on a boundary is not cut off.
4. Prefix each chunk with "<document title> — <section heading>", so the
   chunk's text is self-describing both to the embedder and to the model.
"""

from __future__ import annotations

import re

from aegisdesk.rag.models import Chunk, Document

_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def clean(text: str) -> str:
    """Normalise line endings and whitespace; drop Markdown emphasis markers."""
    text = text.replace("\r\n", "\n").replace("\t", " ")
    text = re.sub(r"[*_]{1,2}([^*_]+)[*_]{1,2}", r"\1", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _sections(body: str) -> list[tuple[str, list[str]]]:
    """[(heading, [paragraph, ...]), ...]; text before the first ## heading goes under the title."""
    sections: list[tuple[str, list[str]]] = []
    heading = ""
    paragraphs: list[str] = []
    for block in body.split("\n\n"):
        block = block.strip()
        if not block:
            continue
        match = _HEADING.match(block.splitlines()[0])
        if match:
            if paragraphs:
                sections.append((heading, paragraphs))
            heading, paragraphs = match.group(2).strip(), []
            rest = "\n".join(block.splitlines()[1:]).strip()
            if rest:
                paragraphs.append(rest)
        else:
            paragraphs.append(block)
    if paragraphs:
        sections.append((heading, paragraphs))
    return sections


def _words(text: str) -> int:
    return len(text.split())


def chunk_document(document: Document, *, max_words: int = 160) -> list[Chunk]:
    meta = document.metadata
    chunks: list[Chunk] = []

    for section, paragraphs in _sections(clean(document.body)):
        # A top-level "# Title" section is named after the document itself.
        label = section if section and section != meta.title else "Overview"
        groups: list[list[str]] = [[]]
        for paragraph in paragraphs:
            current = groups[-1]
            if current and _words(" ".join(current)) + _words(paragraph) > max_words:
                # Start a new chunk, overlapping by the previous paragraph.
                groups.append([current[-1]] if len(current) > 1 else [])
            groups[-1].append(paragraph)

        for group in groups:
            if not group:
                continue
            index = len(chunks) + 1
            chunks.append(
                Chunk(
                    chunk_id=f"{meta.document_id}#{index:02d}",
                    document_id=meta.document_id,
                    index=index,
                    section=label,
                    text=f"{meta.title} — {label}\n" + "\n\n".join(group),
                    metadata=meta,
                )
            )
    return chunks
