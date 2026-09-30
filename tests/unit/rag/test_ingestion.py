"""Loading, cleaning, chunking and the ingestion pipeline."""

from __future__ import annotations

from pathlib import Path

import pytest

from aegisdesk.config import PROJECT_ROOT
from aegisdesk.rag.embeddings import HashingEmbedder
from aegisdesk.rag.ingestion.chunker import chunk_document, clean
from aegisdesk.rag.ingestion.loader import DocumentLoadError, load_directory, load_document
from aegisdesk.rag.ingestion.pipeline import ingest_directory
from aegisdesk.rag.models import Classification
from aegisdesk.rag.store.memory import InMemoryVectorStore

DOCS = PROJECT_ROOT / "data" / "documents"

FRONT_MATTER = """---
document_id: DOC-TST-001
title: Test Guide
version: "1.0"
effective_date: 2026-01-01
department: it
classification: internal
source: tests
---
"""


def _write(directory: Path, name: str, text: str) -> Path:
    path = directory / name
    path.write_text(text, encoding="utf-8")
    return path


def test_corpus_loads_with_required_metadata() -> None:
    documents = load_directory(DOCS)
    assert len(documents) == 12
    by_id = {d.metadata.document_id: d.metadata for d in documents}
    assert by_id["DOC-PDB-001"].classification is Classification.RESTRICTED
    assert by_id["DOC-PDB-001"].allowed_roles == ("it_admin",)
    assert by_id["DOC-FIN-001"].allowed_departments == ("finance",)
    assert by_id["DOC-PWD-001"].classification is Classification.PUBLIC


@pytest.mark.parametrize(
    ("text", "error"),
    [
        ("# no front matter\n", "missing YAML front matter"),
        (FRONT_MATTER.replace("classification: internal", "classification: secret"), "invalid"),
        (FRONT_MATTER.replace("document_id: DOC-TST-001", "document_id: tst1"), "invalid"),
        (FRONT_MATTER.replace("source: tests\n", "source: tests\nowner: me\n"), "invalid"),
    ],
)
def test_invalid_documents_are_rejected(tmp_path: Path, text: str, error: str) -> None:
    with pytest.raises(DocumentLoadError, match=error):
        load_document(_write(tmp_path, "bad.md", text + "\n# Body\ntext\n"))


def test_duplicate_document_ids_are_rejected(tmp_path: Path) -> None:
    _write(tmp_path, "a.md", FRONT_MATTER + "# A\nx\n")
    _write(tmp_path, "b.md", FRONT_MATTER + "# B\ny\n")
    with pytest.raises(DocumentLoadError, match="duplicate"):
        load_directory(tmp_path)


def test_clean_normalises_text() -> None:
    assert clean("a  **bold**\r\n\n\n\nb\t c") == "a bold\n\nb c"


def test_chunks_follow_sections_and_carry_metadata() -> None:
    document = load_document(DOCS / "vpn-troubleshooting.md")
    chunks = chunk_document(document)

    assert [c.chunk_id for c in chunks] == [f"DOC-VPN-001#{i:02d}" for i in range(1, 7)]
    gp512 = next(c for c in chunks if c.section == "Error GP-512")
    assert gp512.text.startswith("VPN Troubleshooting Guide — Error GP-512\n")
    assert "certificate has expired" in gp512.text
    assert all(c.metadata.version == "2.4" for c in chunks)


def test_long_sections_are_split_with_overlap(tmp_path: Path) -> None:
    paragraphs = [f"Paragraph {i} " + "word " * 60 for i in range(4)]
    document = load_document(
        _write(tmp_path, "long.md", FRONT_MATTER + "## Long\n\n" + "\n\n".join(paragraphs))
    )

    chunks = chunk_document(document, max_words=130)

    assert len(chunks) > 1
    assert all(len(c.text.split()) <= 130 + 70 for c in chunks)  # one paragraph of overlap
    # The last paragraph of a chunk is repeated at the start of the next one.
    assert "Paragraph 1" in chunks[0].text and "Paragraph 1" in chunks[1].text


def test_chunk_ids_are_stable_across_runs() -> None:
    document = load_document(DOCS / "it-handbook.md")
    assert chunk_document(document) == chunk_document(document)


def test_ingestion_is_idempotent_and_tracks_changes(tmp_path: Path) -> None:
    for path in DOCS.glob("*.md"):
        _write(tmp_path, path.name, path.read_text(encoding="utf-8"))
    embedder, store = HashingEmbedder(), InMemoryVectorStore()

    first = ingest_directory(tmp_path, embedder, store)
    second = ingest_directory(tmp_path, embedder, store)

    assert len(first.added) == 12 and first.chunks_written == store.chunk_count() == 48
    assert len(second.unchanged) == 12 and second.chunks_written == 0

    vpn = tmp_path / "vpn-troubleshooting.md"
    vpn.write_text(vpn.read_text(encoding="utf-8") + "\n## New section\nExtra text.\n")
    (tmp_path / "remote-working.md").unlink()
    third = ingest_directory(tmp_path, embedder, store)

    assert third.updated == ["DOC-VPN-001"]
    assert third.removed == ["DOC-RW-001"]
    assert "DOC-RW-001" not in store.document_ids()
