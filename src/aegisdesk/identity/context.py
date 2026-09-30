"""Trusted user context.

`UserContext` stands in for the verified claims of an authenticated session
(`sub`, `roles`, `department`, `manager_id`, as in spec section 13). It is
created by application code *before* the model runs and is passed to tools by
the application. The model can neither see how it is built nor change it.

Milestone 1 simulates login by employee ID, validated against the employee
directory. A real identity provider (OIDC) replaces `authenticate` later; the
rest of the code only depends on `UserContext`.
"""

from __future__ import annotations

from dataclasses import dataclass

from aegisdesk.domain.models import EmployeeStatus
from aegisdesk.domain.repository import ServiceDeskRepository


class AuthenticationError(PermissionError):
    pass


@dataclass(frozen=True)
class UserContext:
    employee_id: str
    roles: tuple[str, ...]
    department: str
    manager_id: str | None


def authenticate(repository: ServiceDeskRepository, employee_id: str) -> UserContext:
    """Simulated login: the employee must exist and be active."""
    employee = repository.get_employee(employee_id)
    if employee is None:
        raise AuthenticationError(f"Unknown employee {employee_id!r}")
    if employee.status is not EmployeeStatus.ACTIVE:
        raise AuthenticationError(f"Employee {employee_id!r} is not active")
    return UserContext(
        employee_id=employee.employee_id,
        roles=tuple(employee.roles),
        department=employee.department,
        manager_id=employee.manager_id,
    )
