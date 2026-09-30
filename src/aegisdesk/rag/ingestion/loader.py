"""Load Markdown documents with YAML front matter.

    ---
    document_id: DOC-VPN-001
    title: VPN Troubleshooting Guide
    ...
    ---
    # body in Markdown

Metadata is validated strictly. A document with missing or malformed
metadata is rejected rather than indexed, because an unclassified document
could leak through the access filter.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import ValidationError

from aegisdesk.rag.models import Document, DocumentMetadata


class DocumentLoadError(ValueError):
    pass


def load_document(path: Path) -> Document:
    raw = path.read_text(encoding="utf-8")
    if not raw.startswith("---\n"):
        raise DocumentLoadError(f"{path.name}: missing YAML front matter")
    try:
        _, front_matter, body = raw.split("---\n", 2)
    except ValueError as exc:
        raise DocumentLoadError(f"{path.name}: unterminated front matter") from exc

    try:
        metadata = DocumentMetadata.model_validate(yaml.safe_load(front_matter) or {})
    except ValidationError as exc:
        raise DocumentLoadError(f"{path.name}: invalid metadata: {exc}") from exc

    return Document(
        metadata=metadata,
        body=body,
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )


def load_directory(directory: Path) -> list[Document]:
    documents = [load_document(p) for p in sorted(directory.glob("*.md"))]
    ids = [d.metadata.document_id for d in documents]
    duplicates = {i for i in ids if ids.count(i) > 1}
    if duplicates:
        raise DocumentLoadError(f"duplicate document_id(s): {sorted(duplicates)}")
    return documents
