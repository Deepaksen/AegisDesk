"""Document-level access control for retrieval.

The rule is deterministic and lives in code, not in the prompt:

    public, internal  → every signed-in employee (including contractors)
    confidential      → users with one of `allowed_roles`, or in one of `allowed_departments`
    restricted        → users with one of `allowed_roles` only

It is applied *before* ranking, inside the vector store query. Filtering
after retrieval would be weaker: with top-k = 4, four restricted chunks could
push out every chunk the user may see, and the ranking itself would leak
information about what restricted documents exist.

The pgvector store implements the same rule in SQL; the store contract tests
check that both stores return identical results for the same users.
"""

from __future__ import annotations

from dataclasses import dataclass

from aegisdesk.identity.context import UserContext
from aegisdesk.rag.models import Classification, DocumentMetadata

OPEN_CLASSIFICATIONS = frozenset({Classification.PUBLIC, Classification.INTERNAL})


@dataclass(frozen=True)
class AccessFilter:
    roles: frozenset[str]
    department: str

    @classmethod
    def for_user(cls, user: UserContext) -> AccessFilter:
        return cls(roles=frozenset(user.roles), department=user.department)

    def allows(self, metadata: DocumentMetadata) -> bool:
        if metadata.classification in OPEN_CLASSIFICATIONS:
            return True
        role_match = bool(self.roles & set(metadata.allowed_roles))
        if metadata.classification is Classification.CONFIDENTIAL:
            return role_match or self.department in metadata.allowed_departments
        return role_match  # RESTRICTED
