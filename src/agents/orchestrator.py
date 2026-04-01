"""
OrchestratorAgent — Master pipeline state machine

The orchestrator drives the full client project lifecycle.
It makes NO LLM calls itself — it only loads state, decides what to do next,
and delegates to the appropriate sub-agent.

Call orchestrator.run(client_id) repeatedly to advance the pipeline:
  - From a scheduler (e.g., check every hour via cron or Make)
  - From a webhook (e.g., when a ClickUp task status changes)
  - Manually from the CLI for development/testing

Pipeline tick logic:
  1. Load PipelineState from Notion (client page) and ClickUp (task status)
  2. If pipeline is COMPLETE → return immediately
  3. If current stage is an approval gate AND no approval is logged:
       → Trigger ApprovalHandlerAgent to send notification (if not already sent)
       → Return (pipeline stays blocked until client approves)
  4. If current stage has a mapped agent:
       → Instantiate the agent and call .run(client_id, **stage_kwargs)
       → Write output to Notion
       → Update ClickUp task status to "Complete"
  5. Advance pipeline to the next stage
  6. Create a new ClickUp task for the new stage
  7. Persist updated PipelineState to Notion
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from ..integrations.clickup import ClickUpClient
from ..integrations.notion import NotionClient
from ..models.pipeline import PipelineStage, PipelineState, ApprovalRecord
from .base_agent import AgentError
from .approval_handler import ApprovalHandlerAgent
from .transcript_parser import TranscriptParserAgent
from .mood_board import MoodBoardAgent
from .sitemap import SitemapAgent
from .content import ContentAgent
from .wireframe_spec import WireframeSpecAgent
from .hifi_spec import HifidelitySpecAgent

logger = logging.getLogger(__name__)


# Maps each pipeline stage to the agent class responsible for producing output.
# None = no agent runs; the stage is either manual or an approval wait.
STAGE_AGENT_MAP: dict[PipelineStage, type | None] = {
    PipelineStage.ONBOARDING_COMPLETE: None,          # External trigger (form)
    PipelineStage.MEETING_SCHEDULED: None,            # Manual scheduling
    PipelineStage.MEETING_COMPLETE: TranscriptParserAgent,
    PipelineStage.MOOD_BOARD_DRAFT: MoodBoardAgent,   # ← APPROVAL GATE
    PipelineStage.MOOD_BOARD_APPROVED: None,
    PipelineStage.SITEMAP_DRAFT: SitemapAgent,        # ← APPROVAL GATE
    PipelineStage.SITEMAP_APPROVED: None,
    PipelineStage.CONTENT_DRAFT: ContentAgent,        # ← APPROVAL GATE
    PipelineStage.CONTENT_APPROVED: None,
    PipelineStage.WIREFRAME_DRAFT: WireframeSpecAgent, # ← APPROVAL GATE
    PipelineStage.WIREFRAME_APPROVED: None,
    PipelineStage.HIGH_FID_DRAFT: HifidelitySpecAgent, # ← APPROVAL GATE
    PipelineStage.HIGH_FID_APPROVED: None,
    PipelineStage.WEBFLOW_BUILD: None,                # Manual developer work
    PipelineStage.CLIENT_REVIEW: None,                # ← APPROVAL GATE
    PipelineStage.COMPLETE: None,
}


class OrchestratorAgent:
    """
    Master pipeline orchestrator. Not a sub-class of BaseAgent because
    it makes no LLM calls — it only coordinates other agents.
    """

    def __init__(
        self,
        notion: NotionClient,
        clickup: ClickUpClient,
        model: str,
    ) -> None:
        self.notion = notion
        self.clickup = clickup
        self.model = model
        self.log = logging.getLogger("agent.orchestrator")

    async def run(self, client_id: str, **kwargs: Any) -> dict:
        """
        Execute one tick of the pipeline for a client.

        Returns a dict with:
          - "status": "advanced" | "blocked" | "complete" | "no_action"
          - "stage": current stage after this tick
          - "message": human-readable summary
        """
        state = await self._load_pipeline_state(client_id)

        if state.current_stage == PipelineStage.COMPLETE:
            return {"status": "complete", "stage": state.current_stage.value, "message": "Pipeline is complete."}

        # ── Approval gate check ───────────────────────────────────────────────
        if state.current_stage.requires_approval() and not state.can_advance():
            self.log.info(f"[{client_id}] Blocked at approval gate: {state.current_stage.value}")
            # Notify if not already notified (idempotent)
            await self._handle_approval_gate(state)
            return {
                "status": "blocked",
                "stage": state.current_stage.value,
                "message": f"Waiting for approval at {state.current_stage.value}",
            }

        # ── Run the agent for this stage (if one is mapped) ──────────────────
        agent_class = STAGE_AGENT_MAP.get(state.current_stage)
        if agent_class is not None:
            self.log.info(f"[{client_id}] Running {agent_class.__name__} for stage {state.current_stage.value}")
            agent = agent_class(
                notion=self.notion,
                clickup=self.clickup,
                model=self.model,
            )
            result = await agent.run(client_id, **kwargs)
            await self._update_clickup_task(state, "Complete")
            self.log.info(f"[{client_id}] Agent {agent_class.__name__} completed: {result}")
        else:
            self.log.info(f"[{client_id}] No agent for stage {state.current_stage.value}, advancing.")

        # ── Advance to next stage ─────────────────────────────────────────────
        next_stage = state.current_stage.next_stage()
        if next_stage is None:
            await self._set_stage(state, PipelineStage.COMPLETE)
            return {"status": "complete", "stage": PipelineStage.COMPLETE.value, "message": "Pipeline complete."}

        await self._set_stage(state, next_stage)
        return {
            "status": "advanced",
            "stage": next_stage.value,
            "message": f"Advanced to {next_stage.value}",
        }

    # ── Internal helpers ──────────────────────────────────────────────────────

    async def _load_pipeline_state(self, client_id: str) -> PipelineState:
        """
        Load PipelineState for a client from Notion.

        TODO: Implement by reading the client's Notion page and the
        Client Info DB entry that stores pipeline_stage + stage_history.
        """
        raise NotImplementedError(
            "_load_pipeline_state() not yet implemented. "
            "Requires Notion database IDs from setup_notion.py output."
        )

    async def _set_stage(self, state: PipelineState, new_stage: PipelineStage) -> None:
        """
        Persist a stage change to Notion and create a new ClickUp task.

        TODO: Implement by updating the Client Info DB entry in Notion
        and creating a ClickUp task in the list for the new stage.
        """
        raise NotImplementedError("_set_stage() not yet implemented.")

    async def _handle_approval_gate(self, state: PipelineState) -> None:
        """
        Trigger the ApprovalHandlerAgent to send a notification.
        This should be idempotent — if a notification was already sent, skip.

        TODO: Implement by checking for an existing ApprovalRequest entry in Notion.
        """
        raise NotImplementedError("_handle_approval_gate() not yet implemented.")

    async def _update_clickup_task(self, state: PipelineState, status: str) -> None:
        """Update the current ClickUp task status."""
        if state.clickup_task_id:
            await self.clickup.update_task(state.clickup_task_id, status=status)
