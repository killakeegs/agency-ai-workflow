from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel


class PipelineStage(str, Enum):
    # ── Stage 1: Onboarding ───────────────────────────────────────────────────
    ONBOARDING_COMPLETE = "ONBOARDING_COMPLETE"

    # ── Stage 2: Kickoff meeting ──────────────────────────────────────────────
    MEETING_SCHEDULED = "MEETING_SCHEDULED"
    MEETING_COMPLETE = "MEETING_COMPLETE"        # Transcript parsed, decisions logged in Notion

    # ── Stage 3: Mood board ───────────────────────────────────────────────────
    MOOD_BOARD_DRAFT = "MOOD_BOARD_DRAFT"        # 4–6 variations generated        ← APPROVAL GATE
    MOOD_BOARD_APPROVED = "MOOD_BOARD_APPROVED"

    # ── Stage 4: Sitemap ──────────────────────────────────────────────────────
    SITEMAP_DRAFT = "SITEMAP_DRAFT"              # Page hierarchy generated         ← APPROVAL GATE
    SITEMAP_APPROVED = "SITEMAP_APPROVED"

    # ── Stage 5a: Content ─────────────────────────────────────────────────────
    CONTENT_DRAFT = "CONTENT_DRAFT"              # Per-page copy (AI or provided)   ← APPROVAL GATE
    CONTENT_APPROVED = "CONTENT_APPROVED"

    # ── Stage 5b: Wireframe ───────────────────────────────────────────────────
    WIREFRAME_DRAFT = "WIREFRAME_DRAFT"          # Relume component map per page    ← APPROVAL GATE
    WIREFRAME_APPROVED = "WIREFRAME_APPROVED"

    # ── Stage 5c: High-fidelity design ───────────────────────────────────────
    HIGH_FID_DRAFT = "HIGH_FID_DRAFT"            # Homepage + mobile design brief   ← APPROVAL GATE
    HIGH_FID_APPROVED = "HIGH_FID_APPROVED"

    # ── Stage 5d: Webflow build ───────────────────────────────────────────────
    WEBFLOW_BUILD = "WEBFLOW_BUILD"              # Developer builds in Webflow
    CLIENT_REVIEW = "CLIENT_REVIEW"              # Client reviews staging site      ← APPROVAL GATE
    COMPLETE = "COMPLETE"

    def next_stage(self) -> PipelineStage | None:
        """Returns the next stage in order, or None if this is the final stage."""
        stages = list(PipelineStage)
        idx = stages.index(self)
        return stages[idx + 1] if idx < len(stages) - 1 else None

    def requires_approval(self) -> bool:
        """Returns True if advancing FROM this stage requires explicit client approval."""
        return self in _APPROVAL_GATES


_APPROVAL_GATES: frozenset[PipelineStage] = frozenset({
    PipelineStage.MOOD_BOARD_DRAFT,
    PipelineStage.SITEMAP_DRAFT,
    PipelineStage.CONTENT_DRAFT,
    PipelineStage.WIREFRAME_DRAFT,
    PipelineStage.HIGH_FID_DRAFT,
    PipelineStage.CLIENT_REVIEW,
})


class ApprovalRecord(BaseModel):
    stage: PipelineStage
    approved_by: str                    # "client" | "internal" | agent name
    approved_at: datetime
    feedback: str | None = None
    notion_record_id: str | None = None  # ID of the approval entry in Notion


class PipelineState(BaseModel):
    client_id: str
    current_stage: PipelineStage
    stage_history: list[ApprovalRecord] = []
    clickup_task_id: str | None = None       # Active ClickUp task for current stage
    clickup_folder_id: str | None = None     # ClickUp folder for this client
    notion_client_page_id: str | None = None # Root Notion page for this client
    last_updated: datetime
    is_blocked: bool = False                 # True when waiting at an approval gate
    block_reason: str | None = None

    def can_advance(self) -> bool:
        """
        A stage can advance if it does not require approval,
        OR if an approval record exists for the current stage.
        """
        if not self.current_stage.requires_approval():
            return True
        return any(r.stage == self.current_stage for r in self.stage_history)

    def get_approval(self, stage: PipelineStage) -> ApprovalRecord | None:
        """Returns the approval record for a given stage, if it exists."""
        for record in self.stage_history:
            if record.stage == stage:
                return record
        return None
