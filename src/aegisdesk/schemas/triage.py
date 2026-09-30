"""Structured-output schema for classifying a service-desk message.

This is the first place the model's output becomes *data* that ordinary code
can branch on. It foreshadows the `classify_request` node in the later
LangGraph topology.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class TriageCategory(StrEnum):
    VPN = "vpn"
    HARDWARE = "hardware"
    ACCESS = "access"
    PASSWORD = "password"  # noqa: S105 - a category name, not a credential
    SOFTWARE = "software"
    OTHER = "other"


class Urgency(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class TicketTriage(BaseModel):
    """Classification of an employee's IT service-desk message."""

    category: TriageCategory = Field(description="The IT area the message is about.")
    urgency: Urgency = Field(description="How urgently the employee needs help.")
    summary: str = Field(
        description="One-sentence neutral summary of the problem.", min_length=1, max_length=300
    )
    needs_human: bool = Field(
        description="True if a human technician is likely required to resolve this."
    )
