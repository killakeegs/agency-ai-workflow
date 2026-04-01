from __future__ import annotations

from enum import Enum

from pydantic import BaseModel

from .pipeline import PipelineStage


class NotificationChannel(str, Enum):
    SLACK = "slack"
    EMAIL = "email"
    BOTH = "both"


class NotificationPayload(BaseModel):
    """A notification to be sent when a pipeline stage is ready for approval."""
    client_id: str
    client_name: str
    stage: PipelineStage
    subject: str                        # Email subject / Slack message heading
    body: str                           # Full message body
    notion_page_url: str | None = None  # Link to the deliverable in Notion
    channel: NotificationChannel = NotificationChannel.BOTH

    # For meeting prep notifications
    meeting_agenda: list[str] | None = None  # Agenda items auto-generated for the next call


class ApprovalRequest(BaseModel):
    """Represents a pending approval request sent to a client."""
    client_id: str
    stage: PipelineStage
    sent_at: str                        # ISO datetime string
    sent_via: NotificationChannel
    notion_approval_page_id: str | None = None  # Where approval will be recorded
    is_resolved: bool = False
    resolved_at: str | None = None
