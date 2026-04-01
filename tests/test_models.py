"""
Tests for src/models/

Validates that all Pydantic models construct correctly and that
PipelineStage logic (next_stage, requires_approval) works as expected.
"""
import pytest
from datetime import datetime, timezone


# ── PipelineStage tests ───────────────────────────────────────────────────────

def test_pipeline_stage_next_stage_sequence():
    from src.models.pipeline import PipelineStage

    stages = list(PipelineStage)
    for i, stage in enumerate(stages[:-1]):
        assert stage.next_stage() == stages[i + 1], f"{stage} should advance to {stages[i + 1]}"


def test_pipeline_stage_last_stage_returns_none():
    from src.models.pipeline import PipelineStage

    last = list(PipelineStage)[-1]
    assert last.next_stage() is None


def test_pipeline_stage_approval_gates():
    from src.models.pipeline import PipelineStage

    expected_gates = {
        PipelineStage.MOOD_BOARD_DRAFT,
        PipelineStage.SITEMAP_DRAFT,
        PipelineStage.CONTENT_DRAFT,
        PipelineStage.WIREFRAME_DRAFT,
        PipelineStage.HIGH_FID_DRAFT,
        PipelineStage.CLIENT_REVIEW,
    }
    for stage in PipelineStage:
        if stage in expected_gates:
            assert stage.requires_approval(), f"{stage} should require approval"
        else:
            assert not stage.requires_approval(), f"{stage} should NOT require approval"


def test_pipeline_state_can_advance_without_approval():
    from src.models.pipeline import PipelineStage, PipelineState

    state = PipelineState(
        client_id="test",
        current_stage=PipelineStage.MEETING_COMPLETE,  # no approval required
        last_updated=datetime.now(timezone.utc),
    )
    assert state.can_advance() is True


def test_pipeline_state_blocked_at_gate_without_approval():
    from src.models.pipeline import PipelineStage, PipelineState

    state = PipelineState(
        client_id="test",
        current_stage=PipelineStage.MOOD_BOARD_DRAFT,  # approval required
        last_updated=datetime.now(timezone.utc),
    )
    assert state.can_advance() is False


def test_pipeline_state_can_advance_after_approval():
    from src.models.pipeline import PipelineStage, PipelineState, ApprovalRecord

    approval = ApprovalRecord(
        stage=PipelineStage.MOOD_BOARD_DRAFT,
        approved_by="client",
        approved_at=datetime.now(timezone.utc),
    )
    state = PipelineState(
        client_id="test",
        current_stage=PipelineStage.MOOD_BOARD_DRAFT,
        stage_history=[approval],
        last_updated=datetime.now(timezone.utc),
    )
    assert state.can_advance() is True


# ── Brand models tests ────────────────────────────────────────────────────────

def test_brand_guidelines_constructs():
    from src.models.brand import BrandGuidelines, ColorPalette, ToneOfVoice

    guidelines = BrandGuidelines(
        client_id="test-client",
        colors=ColorPalette(primary="#1A2B3C", secondary="#FFFFFF"),
        tone_of_voice=ToneOfVoice(
            descriptors=["professional", "warm"],
            dos=["Use contractions"],
            donts=["Be overly formal"],
        ),
        primary_font="Inter",
    )
    assert guidelines.colors.primary == "#1A2B3C"
    assert "professional" in guidelines.tone_of_voice.descriptors


# ── Content models tests ──────────────────────────────────────────────────────

def test_sitemap_page_type_defaults():
    from src.models.content import SitemapPage, PageType, ContentMode

    page = SitemapPage(id="p1", title="Home", slug="/", purpose="Landing page")
    assert page.page_type == PageType.STATIC
    assert page.content_mode == ContentMode.AI_GENERATED


def test_sitemap_tree_helpers():
    from src.models.content import Sitemap, SitemapPage

    sitemap = Sitemap(
        client_id="test",
        pages=[
            SitemapPage(id="home", title="Home", slug="/", purpose="Landing", order=0),
            SitemapPage(id="about", title="About", slug="/about", purpose="About us", order=1),
            SitemapPage(id="services", title="Services", slug="/services", purpose="Services", order=2),
            SitemapPage(id="emergency", title="Emergency", slug="/services/emergency",
                       parent_id="services", purpose="Emergency services", order=0),
        ]
    )
    top_level = sitemap.top_level_pages()
    assert len(top_level) == 3
    assert top_level[0].slug == "/"

    children = sitemap.children_of("services")
    assert len(children) == 1
    assert children[0].slug == "/services/emergency"


# ── Client model tests ────────────────────────────────────────────────────────

def test_client_constructs_with_pipeline_state():
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    from src.models.client import Client, ContactInfo, OnboardingData
    from src.models.pipeline import PipelineStage, PipelineState

    state = PipelineState(
        client_id="test-001",
        current_stage=PipelineStage.ONBOARDING_COMPLETE,
        last_updated=datetime.now(timezone.utc),
    )
    client = Client(
        id="test-001",
        contact=ContactInfo(name="Jane", email="jane@test.com", company_name="Test Co"),
        pipeline_state=state,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
    )
    assert client.contact.company_name == "Test Co"
    assert client.pipeline_state.current_stage == PipelineStage.ONBOARDING_COMPLETE


def test_sample_client_fixture_loads():
    """Verify the sample fixture parses into the Client model."""
    import json
    from pathlib import Path
    from datetime import datetime, timezone
    from src.models.client import Client, ContactInfo, OnboardingData
    from src.models.pipeline import PipelineStage, PipelineState

    fixture_path = Path(__file__).parent / "fixtures" / "sample_client.json"
    data = json.loads(fixture_path.read_text())

    contact = ContactInfo(**data["contact"])
    onboarding = OnboardingData(**data["onboarding"])

    assert contact.company_name == "ACME Plumbing"
    assert "Emergency plumbing repairs" in onboarding.services
    assert onboarding.business_type == "local"
