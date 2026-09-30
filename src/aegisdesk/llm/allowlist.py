"""Model allowlist: which provider/model combinations may be used at all."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from aegisdesk.config import ModelProvider


class ModelNotAllowedError(ValueError):
    """Raised when configuration asks for a model that is not allowlisted."""


@dataclass(frozen=True)
class ModelAllowlist:
    allowed: dict[ModelProvider, frozenset[str]]

    @classmethod
    def from_yaml(cls, path: Path) -> ModelAllowlist:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        providers = raw.get("providers") or {}
        allowed: dict[ModelProvider, frozenset[str]] = {}
        for provider_name, models in providers.items():
            # ModelProvider(...) raises ValueError on an unknown provider name.
            allowed[ModelProvider(provider_name)] = frozenset(str(m) for m in models or [])
        return cls(allowed=allowed)

    def is_allowed(self, provider: ModelProvider, model_name: str) -> bool:
        return model_name in self.allowed.get(provider, frozenset())

    def check(self, provider: ModelProvider, model_name: str) -> None:
        if not self.is_allowed(provider, model_name):
            permitted = sorted(self.allowed.get(provider, frozenset()))
            raise ModelNotAllowedError(
                f"Model {model_name!r} is not allowlisted for provider {provider.value!r}. "
                f"Allowed: {permitted}"
            )
