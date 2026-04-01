"""
ApprovalHandlerAgent — All approval gate stages

Triggered at: MOOD_BOARD_DRAFT, SITEMAP_DRAFT, CONTENT_DRAFT,
              WIREFRAME_DRAFT, HIGH_FID_DRAFT, CLIENT_REVIEW

This agent does NOT generate creative content. It handles the human
side of the pipeline: notifying the client that something is ready
for review, logging the approval, and preparing meeting agendas.

Input:
  - Current pipeline stage (which gate just opened)
  - Client contact info (name, email)
  - Notion URL of the deliverable to review
  - ClickUp task ID for the current stage

Processing:
  Notification flow (per stage):
  1. Sends an internal Slack message to #agency-pipeline:
       "Client X's mood board is ready for review. Present at next meeting.
        Notion: [link]"
  2. Sends an external email to the client:
       "Hi [Name], your [deliverable] is ready for your review. [Link]
        We'll walk through it together on our next call."
  3. Updates the ClickUp task status to "Awaiting Approval"
  4. Creates a "Pending Approval" entry in Notion for traceability

  Meeting agenda prep (optional, called before review meetings):
  - Generates a structured meeting agenda based on the deliverable:
      * What we're reviewing today
      * Key decisions the client needs to make
      * Questions to ask / gather feedback on
      * Next steps if approved vs if revisions requested

  Approval recording (called after client approves):
  - Updates the ClickUp task status to "Approved"
  - Creates an ApprovalRecord in Notion with: who approved, when, any feedback
  - Returns the ApprovalRecord so the orchestrator can advance the pipeline

Output:
  - Notification sent (Slack + email)
  - ClickUp task updated
  - Notion approval/pending record created or updated

Tools used:
  - notion_get_page: read client info + deliverable details
  - notion_create_entry: create pending approval / approval record in Notion
  - notion_update_entry: mark approval as resolved
  - clickup_update_task: update task status
  - clickup_add_comment: log notification/approval in task history
"""
from __future__ import annotations

from typing import Any

from .base_agent import BaseAgent
from .tools import APPROVAL_HANDLER_TOOLS
from ..models.pipeline import PipelineStage


class ApprovalHandlerAgent(BaseAgent):
    """Handles client notifications, approval recording, and meeting agenda prep."""

    name = "approval_handler"
    tools = APPROVAL_HANDLER_TOOLS

    async def run(self, client_id: str, **kwargs: Any) -> dict:
        """
        Send approval notification and/or record an approval for a pipeline stage.

        kwargs:
          - stage (PipelineStage): the stage to handle
          - action (str): "notify" | "record_approval"
          - notion_client_page_id (str): root Notion page for this client
          - clickup_task_id (str): ClickUp task for the current stage
          - deliverable_notion_url (str): URL of the deliverable to review
          - approved_by (str, for record_approval): who approved
          - feedback (str, optional): client feedback notes
        """
        # TODO: Implement in Phase 6
        raise NotImplementedError(
            "ApprovalHandlerAgent.run() not yet implemented. "
            "See Phase 6 of the implementation plan."
        )

    async def run_meeting_agenda(self, client_id: str, stage: PipelineStage) -> str:
        """
        Generate a meeting agenda for a review meeting at a given stage.
        Returns the agenda as a formatted string.
        """
        # TODO: Implement in Phase 6
        raise NotImplementedError(
            "ApprovalHandlerAgent.run_meeting_agenda() not yet implemented."
        )

    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        # TODO: Implement tool dispatch
        raise NotImplementedError(f"Tool dispatch not yet implemented: {tool_name}")
