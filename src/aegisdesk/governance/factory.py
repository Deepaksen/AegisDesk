"""Build the gateway from settings: policy file, audit store, environment."""

from __future__ import annotations

from aegisdesk.approvals.evidence import AccessApprovalVerifier
from aegisdesk.audit.events import AuditLog, InMemoryAuditLog
from aegisdesk.config import AuditStoreKind, Settings
from aegisdesk.domain.access_store import AccessStore
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.governance.policy import PolicyEngine


def build_audit_log(settings: Settings) -> AuditLog:
    if settings.audit_store is AuditStoreKind.POSTGRES:
        from aegisdesk.audit.postgres import PgAuditLog

        return PgAuditLog(settings.database_url)
    return InMemoryAuditLog()


def build_gateway(
    settings: Settings,
    *,
    audit: AuditLog | None = None,
    access_store: AccessStore | None = None,
) -> ActionGateway:
    """A policy file that is missing or invalid raises `PolicyError`: startup stops.

    With `access_store`, HIGH-risk provisioning can be allowed once the store
    shows recorded approval; without it, every HIGH-risk call needs approval.
    """
    return ActionGateway(
        PolicyEngine.from_file(settings.policy_path),
        audit if audit is not None else build_audit_log(settings),
        environment=settings.aegis_env.value,
        approvals=AccessApprovalVerifier(access_store) if access_store is not None else None,
    )
