"""Versioned prompts.

Prompts live as data files under `prompts/<name>/v<N>.yaml`, not as string
literals scattered through code. That lets us record exactly which prompt
version produced a result (needed for evaluation later) and review prompt
changes like any other change.

Prompts hold instructions only: no credentials, no environment-specific
configuration and no security policy that the application relies on.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class PromptNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    system: str
    description: str = ""


def load_prompt(prompts_dir: Path, name: str, version: str) -> Prompt:
    path = prompts_dir / name / f"{version}.yaml"
    if not path.is_file():
        raise PromptNotFoundError(f"Prompt {name!r} version {version!r} not found at {path}")

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if raw.get("name") != name or raw.get("version") != version:
        raise ValueError(
            f"{path} declares name={raw.get('name')!r} version={raw.get('version')!r}; "
            f"expected {name!r} {version!r}"
        )
    system = raw.get("system")
    if not isinstance(system, str) or not system.strip():
        raise ValueError(f"{path} has no 'system' text")
    return Prompt(
        name=name,
        version=version,
        system=system.strip(),
        description=str(raw.get("description", "")).strip(),
    )
