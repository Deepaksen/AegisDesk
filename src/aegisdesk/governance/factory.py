"""Build the gateway from settings: policy file, audit store, environment."""

from __future__ import annotations

from aegisdesk.audit.events import AuditLog, InMemoryAuditLog
from aegisdesk.config import AuditStoreKind, Settings
from aegisdesk.governance.gateway import ActionGateway
from aegisdesk.governance.policy import PolicyEngine


def build_audit_log(settings: Settings) -> AuditLog:
    if settings.audit_store is AuditStoreKind.POSTGRES:
        from aegisdesk.audit.postgres import PgAuditLog

        return PgAuditLog(settings.database_url)
    return InMemoryAuditLog()


def build_gateway(settings: Settings, *, audit: AuditLog | None = None) -> ActionGateway:
    """A policy file that is missing or invalid raises `PolicyError`: startup stops."""
    return ActionGateway(
        PolicyEngine.from_file(settings.policy_path),
        audit if audit is not None else build_audit_log(settings),
        environment=settings.aegis_env.value,
    )
