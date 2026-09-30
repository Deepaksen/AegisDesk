from __future__ import annotations

from pathlib import Path

import pytest

from aegisdesk.config import PROJECT_ROOT
from aegisdesk.prompts.loader import PromptNotFoundError, load_prompt

PROMPTS = PROJECT_ROOT / "prompts"


@pytest.mark.parametrize("name", ["assistant", "triage"])
def test_repository_prompts_load(name: str) -> None:
    prompt = load_prompt(PROMPTS, name, "v1")
    assert prompt.name == name
    assert prompt.version == "v1"
    assert prompt.system


def test_missing_version_raises() -> None:
    with pytest.raises(PromptNotFoundError):
        load_prompt(PROMPTS, "triage", "v999")


def test_file_must_declare_matching_name_and_version(tmp_path: Path) -> None:
    (tmp_path / "triage").mkdir()
    (tmp_path / "triage" / "v2.yaml").write_text(
        "name: triage\nversion: v1\nsystem: hi\n", encoding="utf-8"
    )
    with pytest.raises(ValueError, match="expected 'triage' 'v2'"):
        load_prompt(tmp_path, "triage", "v2")


@pytest.mark.parametrize("path", sorted(PROMPTS.glob("*/*.yaml")), ids=lambda p: p.stem)
def test_prompts_contain_no_obvious_secrets(path: Path) -> None:
    text = path.read_text(encoding="utf-8").lower()
    for marker in ("sk-ant-", "api_key", "password=", "localhost", "http://", "https://"):
        assert marker not in text, f"{path} contains {marker!r}"
