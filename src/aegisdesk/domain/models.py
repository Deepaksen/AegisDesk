"""Service-desk domain records.

These are the application's own records, i.e. what the (future) PostgreSQL
tables hold. They are not what the model sees: tools return narrower *view*
schemas (see `aegisdesk.tools.service_desk`) so internal fields never reach
the model by accident.
"""

from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel


class EmployeeStatus(StrEnum):
    ACTIVE = "active"
    TERMINATED = "terminated"


class Employee(BaseModel):
    employee_id: str
    name: str
    email: str
    department: str
    title: str
    roles: list[str]
    manager_id: str | None
    status: EmployeeStatus


class AssetType(StrEnum):
    LAPTOP = "laptop"
    MONITOR = "monitor"
    PHONE = "phone"
    DOCKING_STATION = "docking_station"


class AssetStatus(StrEnum):
    IN_USE = "in_use"
    IN_STOCK = "in_stock"
    IN_REPAIR = "in_repair"
    RETRIEVAL_PENDING = "retrieval_pending"


class Asset(BaseModel):
    asset_tag: str
    asset_type: AssetType
    model: str
    serial_number: str
    status: AssetStatus
    assigned_to: str | None
    assigned_on: date | None


class TicketCategory(StrEnum):
    VPN = "vpn"
    HARDWARE = "hardware"
    ACCESS = "access"
    PASSWORD = "password"  # noqa: S105 - a category name, not a credential
    SOFTWARE = "software"
    OTHER = "other"


class TicketPriority(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TicketStatus(StrEnum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


class Ticket(BaseModel):
    ticket_id: str
    requester_id: str
    title: str
    description: str
    category: TicketCategory
    priority: TicketPriority
    status: TicketStatus
    created_at: datetime
