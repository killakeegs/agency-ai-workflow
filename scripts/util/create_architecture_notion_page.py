#!/usr/bin/env python3
"""
create_architecture_notion_page.py — Create (or refresh) the System Architecture
Notion page under the RxMedia Agency workspace root. Mirror of
docs/ARCHITECTURE.md for team-facing access.

Usage:
    python3 scripts/util/create_architecture_notion_page.py            # Create (refuse if exists)
    python3 scripts/util/create_architecture_notion_page.py --refresh  # Archive old, create new

Canonical source stays in docs/ARCHITECTURE.md; this page is a mirror.
Header callout notes the last-synced date and links back to GitHub.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent / ".env")

from src.config import settings
from src.integrations.notion import NotionClient

WORKSPACE_ROOT = os.environ.get("NOTION_WORKSPACE_ROOT_PAGE_ID", "").strip()
PAGE_TITLE = "System Architecture — Agency Workflow"
GITHUB_URL = "https://github.com/killakeegs/agency-ai-workflow/blob/main/docs/ARCHITECTURE.md"


# ── Block builders ─────────────────────────────────────────────────────────────

def _rt(text: str, bold: bool = False, code: bool = False, link: str | None = None) -> dict:
    text_obj: dict = {"content": text}
    if link:
        text_obj["link"] = {"url": link}
    return {
        "type": "text",
        "text": text_obj,
        "annotations": {"bold": bold, "code": code},
    }


def p(*rich: dict) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": list(rich)}}


def h1(text: str) -> dict:
    return {"object": "block", "type": "heading_1", "heading_1": {"rich_text": [_rt(text)]}}


def h2(text: str) -> dict:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": [_rt(text)]}}


def h3(text: str) -> dict:
    return {"object": "block", "type": "heading_3", "heading_3": {"rich_text": [_rt(text)]}}


def bullet(text: str, *extra_rich: dict) -> dict:
    rich = [_rt(text)] if not extra_rich else [_rt(text)] + list(extra_rich)
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": rich},
    }


def bullet_rich(*rich: dict) -> dict:
    return {
        "object": "block",
        "type": "bulleted_list_item",
        "bulleted_list_item": {"rich_text": list(rich)},
    }


def numbered(text: str) -> dict:
    return {
        "object": "block",
        "type": "numbered_list_item",
        "numbered_list_item": {"rich_text": [_rt(text)]},
    }


def callout(text: str, emoji: str = "📘", color: str = "blue_background") -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": [_rt(text)],
            "icon": {"emoji": emoji},
            "color": color,
        },
    }


def callout_rich(emoji: str, color: str, *rich: dict) -> dict:
    return {
        "object": "block",
        "type": "callout",
        "callout": {
            "rich_text": list(rich),
            "icon": {"emoji": emoji},
            "color": color,
        },
    }


def divider() -> dict:
    return {"object": "block", "type": "divider", "divider": {}}


def toggle(summary: str, children: list[dict]) -> dict:
    return {
        "object": "block",
        "type": "toggle",
        "toggle": {"rich_text": [_rt(summary)], "children": children},
    }


def code(text: str, language: str = "plain text") -> dict:
    return {
        "object": "block",
        "type": "code",
        "code": {"rich_text": [_rt(text)], "language": language},
    }


# ── Page content ───────────────────────────────────────────────────────────────

def build_blocks() -> list[dict]:
    today = date.today().isoformat()
    blocks: list[dict] = []

    # Header: sync note
    blocks.append(callout_rich(
        "📘", "blue_background",
        _rt("Canonical source: "),
        _rt("docs/ARCHITECTURE.md", code=True),
        _rt(" in the agency repo. This page is a mirror for team reference. "),
        _rt("View on GitHub", link=GITHUB_URL),
        _rt(f" · Last synced: {today}."),
    ))

    blocks.append(p(_rt(
        "Read this when you need to: understand where a new piece of "
        "functionality should live; decide whether something is an agent, service, "
        "or integration; trace why something broke and what else might be affected; "
        "onboard a new person (or a new Claude Code session) to the full picture."
    )))

    blocks.append(divider())

    # Section 1 — The 6-Layer Hierarchy
    blocks.append(h1("The 6-Layer Hierarchy"))

    blocks.append(callout_rich(
        "⚡", "yellow_background",
        _rt("The rule: ", bold=True),
        _rt("dependencies flow up only. A higher layer may import from any layer below it. "
            "A lower layer must never import from a higher layer. This is what keeps the "
            "system modular as it scales."),
    ))

    # Diagram as code block (preserves the ASCII art)
    blocks.append(code(
        "Layer 6  User Interfaces     (Rex, Make commands, menu, direct scripts)\n"
        "              ↑ triggers\n"
        "Layer 5  Orchestrators       (Railway crons, Make scripts, Rex dispatchers)\n"
        "              ↑ invokes\n"
        "Layer 4  Agents              (LLM-driven, one primary DB each)\n"
        "              ↑ consumes\n"
        "Layer 3  Services            (shared business logic)\n"
        "              ↑ calls\n"
        "Layer 2  Integrations        (pure API wrappers)\n"
        "              ↑ speaks to\n"
        "Layer 1  Data                (Notion + external APIs)",
    ))

    # Layer 1
    blocks.append(h2("Layer 1 — Data"))
    blocks.append(p(_rt(
        "Notion is the single source of truth. All structured client data lives in "
        "Notion databases. Everything else is a data source we read from (and "
        "occasionally write to)."
    )))
    blocks.append(h3("Notion structure per client"))
    blocks.extend([
        bullet_rich(_rt("Client Info DB", bold=True), _rt(" — pipeline stage, contacts, services, vertical, template")),
        bullet_rich(_rt("Client Log DB", bold=True), _rt(" — chronological timeline of every interaction")),
        bullet_rich(_rt("Brand Guidelines DB", bold=True), _rt(" — voice, colors, fonts, photography style, reviewer info")),
        bullet_rich(_rt("Business Profile page", bold=True), _rt(" — deep client knowledge, 12+ sections")),
        bullet_rich(_rt("Care Plan DB", bold=True), _rt(" — monthly PageSpeed + ADA (if care plan active)")),
    ])
    blocks.append(h3("Service-specific DBs (when stage starts)"))
    blocks.extend([
        bullet("Sitemap DB, Page Content DB, Images DB — website build"),
        bullet("Keywords DB, Competitors DB, SEO Metrics DB — SEO clients"),
        bullet("Blog Posts DB, Social Posts DB, GBP Posts DB — content retainer clients"),
    ])
    blocks.append(h3("Workspace-level DBs (shared across clients)"))
    blocks.extend([
        bullet("Clients DB — master registry"),
        bullet("Meeting Transcripts DB — Gemini Notes by Gemini dumps here + mirror of Notion AI entries"),
        bullet("Flags DB — blockers, open actions, risks, wins across all clients"),
        bullet("Email Monitor State DB — monitor cursor + alerted thread dedup cache"),
    ])
    blocks.append(h3("External data sources"))
    blocks.extend([
        bullet("Gmail, Google Calendar, Google Drive (Gemini meet notes)"),
        bullet("Google Search Console, Analytics 4, Business Profile (planned SEO agent consumption)"),
        bullet("ClickUp, DataForSEO, Search Atlas"),
        bullet("Replicate (Flux Schnell), Pexels, Slack, Webflow"),
    ])

    # Layer 2
    blocks.append(h2("Layer 2 — Integrations"))
    blocks.append(p(
        _rt("Path: "), _rt("src/integrations/", code=True),
        _rt(". Pure API wrappers. No business logic."),
    ))
    blocks.extend([
        bullet_rich(_rt("notion.py", code=True), _rt(" — raw HTTP client (bypasses broken SDK v3 methods)")),
        bullet_rich(_rt("clickup.py", code=True), _rt(" — task create, workspace browse")),
        bullet_rich(_rt("gmail.py", code=True), _rt(" — OAuth, search, fetch, thread summarize, noise filter")),
        bullet_rich(_rt("google_calendar.py", code=True), _rt(" — event lookup by time, attendee extraction")),
        bullet_rich(_rt("google_drive.py", code=True), _rt(" — list files in folder, fetch doc text (added with Gemini swap)")),
        bullet_rich(_rt("business_profile.py", code=True), _rt(" — Notion page-block specific; loads + updates Business Profile")),
    ])

    # Layer 3
    blocks.append(h2("Layer 3 — Services"))
    blocks.append(p(
        _rt("Path: "), _rt("src/services/", code=True),
        _rt(". Shared business logic, reusable across agents and orchestrators."),
    ))
    blocks.extend([
        bullet_rich(
            _rt("email_enrichment.py", code=True),
            _rt(" — thread synthesis, dedup, Client Log writing, profile enrichment, flag writing. Consumed by email monitor + backfill."),
        ),
        bullet_rich(
            _rt("style_reference.py", code=True),
            _rt(" — feedback-loop service. Agents log approved/rejected outputs to a Style Reference DB; future runs pull recent examples to prime per-client voice. "),
            _rt("Active:", bold=True),
            _rt(" ContentAgent + BlogAgent both prime from it. Auto-sweeps Content DB and Blog Posts DB approvals. Not yet consumed by SocialAgent or future SEO agents."),
        ),
        bullet_rich(
            _rt("gemini_meeting.py", code=True),
            _rt(" — parses Gemini meeting docs (title/date/attendees/body). Added with 2026-04-22 swap."),
        ),
    ])

    # Layer 4
    blocks.append(h2("Layer 4 — Agents"))
    blocks.append(p(
        _rt("Path: "), _rt("src/agents/", code=True),
        _rt(". LLM-driven. One "), _rt("run()", code=True),
        _rt(" method per agent. One primary Notion DB per agent. Inherit "),
        _rt("BaseAgent", code=True), _rt("."),
    ))
    blocks.append(h3("Built (4 agents)"))
    blocks.extend([
        bullet_rich(_rt("OnboardingAgent", bold=True), _rt(" → Client Info DB (+ 3 more). Trigger: "), _rt("make onboard", code=True)),
        bullet_rich(_rt("SitemapAgent", bold=True), _rt(" → Sitemap DB. Trigger: "), _rt("make sitemap", code=True)),
        bullet_rich(_rt("ContentAgent", bold=True), _rt(" → Page Content DB. Trigger: "), _rt("make content", code=True)),
        bullet_rich(_rt("ImageGenerationAgent", bold=True), _rt(" → Images DB. Trigger: "), _rt("make images-brand / make images-pages", code=True)),
    ])
    blocks.append(h3("Planned"))
    blocks.extend([
        bullet_rich(
            _rt("7 SEO agents", bold=True),
            _rt(" per the Notion SEO plan: LocalSEO (flagship), KeywordResearch, OnPageSEO, "
                "TechnicalSEO, ContentStrategy, Content (existing, expanded), Strategy. "),
            _rt("Blocked on Andrea review (8 open questions).", bold=True),
        ),
        bullet_rich(_rt("BlogAgent", bold=True), _rt(" → would own Blog Posts DB (currently 4 scripts instead)")),
        bullet_rich(_rt("SocialAgent", bold=True), _rt(" → would own Social Posts DB (currently 2 scripts instead)")),
        bullet_rich(_rt("PaidAdsAgent", bold=True), _rt(" → greenfield, zero code yet")),
    ])

    blocks.append(h3("Content-generating agents — scope rule"))
    blocks.append(callout_rich(
        "⚡", "yellow_background",
        _rt("ContentAgent, BlogAgent, and SocialAgent are siblings, not modes of one agent.", bold=True),
    ))
    blocks.append(p(_rt(
        "The single ContentAgent generates website copy only (Sitemap → Page Content DB). "
        "It does NOT handle blog posts, social posts, GBP posts, or email copy. "
        "Those are separate agents because:"
    )))
    blocks.extend([
        bullet_rich(_rt("Voice is fundamentally different — ", bold=True),
                    _rt("website speaks as the practice; blog is first-person clinician; social is platform-punchy. Merging into one system prompt degrades all three outputs.")),
        bullet_rich(_rt("Approval gates differ — ", bold=True),
                    _rt("blog has medical reviewer attribution (YMYL E-E-A-T); social has none; website has sitemap/content approval cycles.")),
        bullet_rich(_rt("Cadence differs — ", bold=True),
                    _rt("website is one-time per project; blog is quarterly batches; social is weekly/monthly.")),
        bullet_rich(_rt("Publishing targets differ — ", bold=True),
                    _rt("website → Webflow static; blog → Webflow CMS; social → IG/FB/LinkedIn/GBP APIs.")),
        bullet_rich(_rt("Style Reference signals collide if merged — ", bold=True),
                    _rt("feedback on blog-style copy would train website voice incorrectly. Per-agent scoping prevents this.")),
    ])
    blocks.append(h3("Guardrails"))
    blocks.extend([
        numbered("Don't add blog or social generation to ContentAgent as a mode. If that impulse comes up, push back."),
        numbered("BlogAgent and SocialAgent are siblings — they share BaseAgent, not ContentAgent. No parent/child inheritance between content agents."),
        numbered("One primary DB per agent. Page Content, Blog Posts, Social Posts are three DBs → three agents."),
        numbered("When cross-cutting logic emerges (brand voice loading, SEO rules, structural-sections rule, Notion block formatting), extract to src/services/ so all three can share it. Don't duplicate via inheritance."),
    ])
    blocks.append(callout_rich(
        "💡", "gray_background",
        _rt("Shared-service extraction timing: ", bold=True),
        _rt("do NOT extract a shared content_generation service on day one. Let BlogAgent ship first. "
            "Once the overlapping patterns with ContentAgent are obvious from real code, extract the shared parts. "
            "Premature abstraction forces guesses about the shape."),
    ))
    blocks.append(callout_rich(
        "⚠️", "yellow_background",
        _rt("SocialAgent caveat: ", bold=True),
        _rt("starting as one agent with platform as a mode (IG/FB, LinkedIn, GBP). "
            "These platforms have genuinely different voices — if calibration starts fighting itself "
            "(LinkedIn output drifting toward IG tone or vice versa), that's the signal to split into "
            "platform-specific agents. Watch for it, don't pre-split."),
    ))

    # Layer 5
    blocks.append(h2("Layer 5 — Orchestrators"))
    blocks.append(p(_rt("Railway crons, Make scripts, Rex tool dispatchers.")))
    blocks.append(h3("Railway crons"))
    blocks.extend([
        bullet_rich(_rt("Email Monitor", bold=True), _rt(" — every 15 min. Routes new emails, Client Log, flags.")),
        bullet_rich(_rt("Gemini Meeting Processor", bold=True), _rt(" — every 5 min. Polls Drive for Gemini Notes, writes Client Log, Gmail draft, ClickUp tasks.")),
        bullet_rich(_rt("Morning Briefing", bold=True), _rt(" — 7am PST. Agency pulse + per-team-member overdue DMs.")),
        bullet_rich(_rt("Meeting Prep", bold=True), _rt(" — per calendar lookup. Prep docs for today's meetings.")),
        bullet_rich(_rt("Care Plan", bold=True), _rt(" — 1st of month, 4am CT. Monthly PageSpeed + care plan report per client.")),
    ])
    blocks.append(h3("Make commands + Rex tool dispatchers"))
    blocks.extend([
        bullet("make targets — on-demand pipeline stage invocation"),
        bullet("rex/tools/* — Notion, ClickUp, Pipeline, Meeting, Email dispatchers"),
    ])

    # Layer 6
    blocks.append(h2("Layer 6 — User Interfaces"))
    blocks.extend([
        bullet_rich(_rt("Rex", bold=True), _rt(" (Slack) — conversational multi-tool LLM. DM or @mention. Deploy: Railway. Tools: 5 modules.")),
        bullet_rich(_rt("Make commands", bold=True), _rt(" — "), _rt("make onboard CLIENT=x", code=True), _rt(", etc.")),
        bullet_rich(_rt("Interactive menu", bold=True), _rt(" — "), _rt("make run", code=True), _rt(" for newcomer-friendly test runs.")),
        bullet_rich(_rt("Direct script invocation", bold=True), _rt(" — dev only.")),
    ])
    blocks.append(callout_rich(
        "💡", "gray_background",
        _rt("Rex is NOT an agent.", bold=True),
        _rt(" It's a dispatcher that exposes a multi-tool LLM conversation layer over "
            "the existing agents + services + integrations. When an agent is built, "
            "it gets a Rex tool definition so the team can invoke it conversationally."),
    ))

    blocks.append(divider())

    # Per-domain build status
    blocks.append(h1("Per-Domain Build Status"))
    blocks.extend([
        bullet_rich(_rt("Website Build: ", bold=True), _rt("✅ 4 agents + 20+ scripts. Minor: Webflow CMS push pending.")),
        bullet_rich(_rt("SEO: ", bold=True), _rt("📋 0 agents built, 7 planned. Blocked on Andrea review.")),
        bullet_rich(_rt("Blog: ", bold=True), _rt("📋 0 agents built, 1 planned. 4 scripts exist.")),
        bullet_rich(_rt("Social: ", bold=True), _rt("📋 0 agents built, 1 planned. 2 scripts exist.")),
        bullet_rich(_rt("Email Enrichment: ", bold=True), _rt("✅ Service + Railway cron. Complete.")),
        bullet_rich(_rt("Meeting Ops: ", bold=True), _rt("✅ Railway cron + Rex tool. Gemini-first since 2026-04-22.")),
        bullet_rich(_rt("Care Plan: ", bold=True), _rt("✅ Monthly report cron.")),
        bullet_rich(_rt("Onboarding: ", bold=True), _rt("✅ 1 agent + 4 scripts.")),
        bullet_rich(_rt("Paid Ads: ", bold=True), _rt("❌ Zero code. Agent planned.")),
    ])

    blocks.append(divider())

    # Known debt
    blocks.append(h1("Known Architectural Debt"))
    blocks.append(p(_rt(
        "Three places where current code bends the hierarchy rules. Worth fixing, but "
        "not urgent. Do NOT refactor preemptively."
    )))
    blocks.extend([
        numbered("OnboardingAgent writes to 4+ DBs — violates one-DB rule. Fix by splitting into provision / register / notify. Defer until next re-run failure."),
        numbered("ContentAgent does copy AND Notion block-formatting. Move formatter to src/integrations/notion_blocks.py. Low risk, good warm-up refactor."),
        numbered("SEO script logic mixes layers. Extract into src/services/seo/ WHEN SEOAgent build starts — not before. Andrea's review may reshape architecture."),
    ])

    blocks.append(divider())

    # Conventions
    blocks.append(h1("Conventions Worth Knowing"))
    blocks.extend([
        bullet_rich(
            _rt("Notion callout blocks mark team-only metadata. ", bold=True),
            _rt("ContentAgent wraps SEO summaries + Internal Notes in callouts tagged with "),
            _rt('"(team-only — not user-facing)"', code=True),
            _rt(". Renderers detect and skip."),
        ),
        bullet_rich(
            _rt("Divider headings structure content. ", bold=True),
            _rt("ContentAgent uses "),
            _rt("── Hero Section ──", code=True),
            _rt(", "),
            _rt("── Page Sections ──", code=True),
            _rt(", "),
            _rt("── FAQs ──", code=True),
            _rt(" as zone transitions. "),
            _rt("── SEO ──", code=True),
            _rt(", "),
            _rt("── Internal Notes ──", code=True),
            _rt(" are discard markers."),
        ),
        bullet_rich(
            _rt("Config-driven personalization. ", bold=True),
            _rt("Per-vertical lives in "),
            _rt("config/", code=True),
            _rt(", never in agent prompts. Prompts reference config at runtime."),
        ),
        bullet_rich(
            _rt("Flags DB is the workspace-wide signal layer. ", bold=True),
            _rt("Blockers, open actions, wins, scope changes → one DB with lifecycle. All write here; Rex reads here."),
        ),
        bullet_rich(
            _rt("Per-client Slack channels. ", bold=True),
            _rt("Alerts route to the client's channel (e.g. #crown), not a generic #agency-pipeline."),
        ),
        bullet_rich(
            _rt("Flags are pull, not push. ", bold=True),
            _rt("Flags live in the Notion Flags DB. Rex answers \"what's open for X?\" on demand; "
                "the morning briefing covers proactive awareness. Do NOT build Slack posts, daily digests, "
                "or other push notifications for routine (non-urgent) flags — they create noise without value. "
                "Exception: 🚨 URGENT EMAIL alerts (keyword-triggered, inbound-only, deduped) and similar "
                "genuinely time-sensitive signals. Everything else is pull."),
        ),
    ])

    blocks.append(divider())

    # How to add
    blocks.append(h1("How to Add Things Cleanly"))
    blocks.append(toggle("New integration", [
        numbered("Create src/integrations/<name>.py"),
        numbered("Pure API wrapper, no business logic"),
        numbered("Environment variable for auth in .env, document in CLAUDE.md"),
    ]))
    blocks.append(toggle("New service", [
        numbered("Create src/services/<name>.py"),
        numbered("Stateless functions preferred"),
        numbered("Imports from Layer 2 only"),
        numbered("Pre-check: is this logic used by 2+ consumers? If no, it's probably still a script."),
    ]))
    blocks.append(toggle("New agent", [
        numbered("Create src/agents/<name>.py, inherit BaseAgent"),
        numbered("One run() method, one primary DB"),
        numbered("Prompts + templates → config/"),
        numbered("Add Makefile target"),
        numbered("Add Rex tool definition if team-callable"),
        numbered("Document in CLAUDE.md"),
        numbered("Support dry-run mode"),
    ]))
    blocks.append(toggle("New orchestrator (cron or make command)", [
        numbered("Decide: Railway cron vs Make command vs Rex trigger"),
        numbered("Script in scripts/<category>/"),
        numbered("Never put business logic in the script — call services + agents"),
        numbered("If Railway cron: document schedule + command in CLAUDE.md"),
    ]))
    blocks.append(toggle("New Rex tool", [
        numbered("Add handler function in rex/tools/<category>.py"),
        numbered("Register tool definition in rex/app.py"),
        numbered("Keep handler thin — call agents/services, don't reimplement their logic"),
    ]))

    blocks.append(divider())

    # Related plans
    blocks.append(h1("Related Plans"))
    blocks.extend([
        bullet_rich(_rt("SEO strategic plan (Notion)", link="https://www.notion.so/349f7f45333e816fa756dacd373f21f2"),
                    _rt(" — 7-agent architecture, 3-release rollout, in review with Andrea.")),
        bullet_rich(_rt("GitHub: agency-ai-workflow", link="https://github.com/killakeegs/agency-ai-workflow"),
                    _rt(" — code + docs/ARCHITECTURE.md canonical source.")),
    ])

    blocks.append(divider())

    # Maintenance
    blocks.append(h1("Maintenance"))
    blocks.append(p(_rt("Update this page (and the canonical ARCHITECTURE.md in the repo) when:")))
    blocks.extend([
        bullet("A new agent is built"),
        bullet("A new integration is added"),
        bullet("A new Railway cron is deployed"),
        bullet("Architectural debt is paid down"),
        bullet("A major plan shifts from planned → built"),
    ])
    blocks.append(p(_rt("Do NOT update for prompt tweaks, schema additions, or one-off scripts. "
                       "Those live in commit messages.")))

    blocks.append(callout_rich(
        "⚙️", "gray_background",
        _rt("To refresh this page: "),
        _rt("python3 scripts/util/create_architecture_notion_page.py", code=True),
        _rt(" (re-creates from the latest docs/ARCHITECTURE.md). Currently manual; could be "
            "wired into CI later if drift becomes a problem."),
    ))

    return blocks


# ── Page create/update ─────────────────────────────────────────────────────────

async def find_existing_page(notion: NotionClient) -> str | None:
    """Search for an existing 'System Architecture' page under the workspace root."""
    results = await notion._client.request(
        path="search",
        method="POST",
        body={"query": PAGE_TITLE, "filter": {"value": "page", "property": "object"}},
    )
    for r in results.get("results", []):
        title_parts = r.get("properties", {}).get("title", {}).get("title", [])
        title = "".join(p.get("text", {}).get("content", "") for p in title_parts)
        if title == PAGE_TITLE:
            return r["id"]
    return None


async def create_page(notion: NotionClient, blocks: list[dict]) -> str:
    """Create the page under the workspace root with all blocks."""
    result = await notion._client.request(
        path="pages",
        method="POST",
        body={
            "parent": {"type": "page_id", "page_id": WORKSPACE_ROOT},
            "properties": {
                "title": {"title": [{"type": "text", "text": {"content": PAGE_TITLE}}]}
            },
            "children": blocks[:100],  # Notion caps at 100 blocks per request
        },
    )
    return result["id"]


async def append_remaining_blocks(notion: NotionClient, page_id: str, blocks: list[dict]) -> None:
    """Append any blocks beyond the first 100 via patch-block-children."""
    remaining = blocks[100:]
    while remaining:
        chunk = remaining[:100]
        remaining = remaining[100:]
        await notion._client.request(
            path=f"blocks/{page_id}/children",
            method="PATCH",
            body={"children": chunk},
        )


async def archive_page(notion: NotionClient, page_id: str) -> None:
    """Move the existing page to trash (Notion's 'archive') so we can create a fresh one."""
    await notion._client.request(
        path=f"pages/{page_id}",
        method="PATCH",
        body={"in_trash": True},
    )


async def main(refresh: bool = False):
    if not WORKSPACE_ROOT:
        print("ERROR: NOTION_WORKSPACE_ROOT_PAGE_ID not set.")
        sys.exit(1)

    notion = NotionClient(api_key=settings.notion_api_key)

    blocks = build_blocks()
    print(f"Building {len(blocks)} blocks...")

    existing = await find_existing_page(notion)
    if existing:
        if refresh:
            print(f"Existing page found: {existing}")
            print("  Archiving old page...")
            await archive_page(notion, existing)
            print("  ✓ Archived.")
        else:
            print(f"Existing page found: {existing}")
            print("⚠ Not overwriting. Re-run with --refresh to archive + create new.")
            return

    page_id = await create_page(notion, blocks)
    print(f"✓ Created page: {page_id}")

    if len(blocks) > 100:
        print(f"Appending remaining {len(blocks) - 100} blocks...")
        await append_remaining_blocks(notion, page_id, blocks)
        print(f"✓ All {len(blocks)} blocks written.")

    url = f"https://www.notion.so/{page_id.replace('-', '')}"
    print(f"\n🔗 {url}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Archive the existing page (if found) and create a fresh one",
    )
    args = parser.parse_args()
    asyncio.run(main(refresh=args.refresh))
