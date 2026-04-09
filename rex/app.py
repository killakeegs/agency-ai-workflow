"""
Rex — RxMedia's AI Slack agent.

Powered by Claude (claude-sonnet-4-6). Reads live data from Notion.
Handles Slack DMs and @mentions.

Deploy to Railway:
  1. Connect this repo in Railway
  2. Set env vars: SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET, ANTHROPIC_API_KEY,
     NOTION_API_KEY, NOTION_WORKSPACE_ROOT_PAGE_ID, CLICKUP_API_KEY,
     CLICKUP_WORKSPACE_ID
  3. Railway auto-runs: uvicorn rex.app:api --host 0.0.0.0 --port $PORT
  4. Update Slack Event Subscriptions URL to: https://<your-railway-url>/slack/events
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

# Add project root so we can import existing src/ modules
sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
from fastapi import FastAPI, Request
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

from config.clients import CLIENTS
from src.config import settings
from src.integrations.notion import NotionClient


# ── Initialize clients ────────────────────────────────────────────────────────

slack_app = AsyncApp(
    token=os.environ.get("SLACK_BOT_TOKEN") or settings.slack_bot_token,
    signing_secret=os.environ.get("SLACK_SIGNING_SECRET"),
)
claude = anthropic.AsyncAnthropic(api_key=settings.anthropic_api_key)
notion = NotionClient(settings.notion_api_key)


# ── Notion property helpers ───────────────────────────────────────────────────

def _text(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("rich_text", []))

def _title(prop: dict) -> str:
    return "".join(p.get("text", {}).get("content", "") for p in prop.get("title", []))

def _select(prop: dict) -> str:
    sel = prop.get("select")
    return sel.get("name", "") if sel else ""


# ── System prompt ─────────────────────────────────────────────────────────────

def _build_system_prompt() -> str:
    client_lines = "\n".join(
        f"  • {key} — {cfg.get('name', key)}"
        for key, cfg in CLIENTS.items()
    )
    return f"""You are Rex, the AI knowledge agent for RxMedia — a digital marketing agency that builds AI-powered website workflows for clients.

Your job: help the RxMedia team understand the agency workflow, client project status, and the tools being built. You have access to live Notion data via tools — use them when someone asks about specific client content, sitemap pages, action items, or pipeline status.

━━ AGENCY PIPELINE ━━
Each client goes through these stages (in order):
1. Onboarding — Notion DBs + ClickUp provisioned automatically
2. Kickoff Meeting — transcript parsed, brand preferences extracted
3. Sitemap — page hierarchy built, client approves before proceeding
4. Content — per-page copy + SEO written, client approves
5. Stock Photos — Pexels images curated and approved
6. Wireframe — Relume component map built, client approves
7. Webflow Build — developer builds in Webflow
8. Live — launched

Approval gates exist between stages. The pipeline never auto-advances without a logged client approval.

━━ KEY TOOLS & INTEGRATIONS ━━
• Notion — central knowledge base (all client data lives here)
• ClickUp — pipeline state + approval tasks
• Relume — AI component library; generates Webflow-compatible wireframes from a sitemap
• Replicate (Flux Schnell) — AI image generation for brand/page images
• Pexels — stock photography (CC0, no attribution required)
• Webflow — final website delivery and client editing
• Make.com — workflow automation
• Claude (Anthropic) — AI orchestration engine for all agents

━━ CLIENTS ━━
{client_lines}

Active client: Summit Therapy — pediatric speech therapy, OT, and PT clinic in Frisco and McKinney, TX. Currently in Webflow developer build stage (handed off April 2026).

━━ HOW TO ANSWER ━━
• For "how does X work" questions — answer from your knowledge above
• For "what pages are in the sitemap", "what's the content for X page", "what action items are open" — use a tool to pull live Notion data
• Don't invent specific data — if you're not sure, use a tool or say so

━━ SLACK FORMATTING ━━
• Use *bold* for key terms and section headers
• Use bullet points with • for lists
• Keep it concise — 3-6 lines max unless a detailed breakdown is explicitly asked for
• No filler phrases (no "Great question!", "Certainly!", etc.)
• Reply directly and confidently"""


SYSTEM_PROMPT = _build_system_prompt()


# ── Tool schemas ──────────────────────────────────────────────────────────────

TOOLS = [
    {
        "name": "list_clients",
        "description": "List all agency clients with their names and client keys. Use this to find the right client_key before calling other tools.",
        "input_schema": {
            "type": "object",
            "properties": {},
            "required": [],
        },
    },
    {
        "name": "get_pipeline_status",
        "description": "Get the current pipeline stage and status for a client from Notion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {
                    "type": "string",
                    "description": "The client identifier, e.g. 'summit_therapy'",
                },
            },
            "required": ["client_key"],
        },
    },
    {
        "name": "get_sitemap",
        "description": "Get all pages in a client's sitemap — page name, parent, slug, type (Static/CMS), and key sections.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {
                    "type": "string",
                    "description": "The client identifier, e.g. 'summit_therapy'",
                },
            },
            "required": ["client_key"],
        },
    },
    {
        "name": "get_page_content",
        "description": "Get content from a client's Content DB — title tag, meta description, H1, and body copy. Optionally filter by page name.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {
                    "type": "string",
                    "description": "The client identifier, e.g. 'summit_therapy'",
                },
                "page_name": {
                    "type": "string",
                    "description": "Optional: filter to a specific page, e.g. 'Home', 'About Us'",
                },
            },
            "required": ["client_key"],
        },
    },
    {
        "name": "get_action_items",
        "description": "Get action items for a client from Notion. Optionally filter by assignee (Agency or Client).",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {
                    "type": "string",
                    "description": "The client identifier, e.g. 'summit_therapy'",
                },
                "assignee": {
                    "type": "string",
                    "description": "Optional: 'Agency' or 'Client' to filter by assignee",
                },
            },
            "required": ["client_key"],
        },
    },
]


# ── Tool execution ────────────────────────────────────────────────────────────

async def _execute_tool(name: str, tool_input: dict) -> str:
    try:
        if name == "list_clients":
            lines = [f"{key} — {cfg.get('name', key)}" for key, cfg in CLIENTS.items()]
            return "\n".join(lines)

        elif name == "get_pipeline_status":
            client_key = tool_input["client_key"]
            if client_key not in CLIENTS:
                return f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}"
            cfg = CLIENTS[client_key]
            entries = await notion.query_database(cfg["client_info_db_id"])
            if not entries:
                return "No client info found in Notion."
            pp = entries[0]["properties"]
            stage = _select(pp.get("Pipeline Stage", {}))
            status = _select(pp.get("Stage Status", {}))
            notes = _text(pp.get("Revision Notes", {}))
            return (
                f"Client: {cfg.get('name', client_key)}\n"
                f"Stage: {stage or 'Not set'}\n"
                f"Status: {status or 'Not set'}\n"
                f"Notes: {notes or 'None'}"
            )

        elif name == "get_sitemap":
            client_key = tool_input["client_key"]
            if client_key not in CLIENTS:
                return f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}"
            cfg = CLIENTS[client_key]
            entries = await notion.query_database(
                cfg["sitemap_db_id"],
                sorts=[{"property": "Order", "direction": "ascending"}],
            )
            if not entries:
                return "No sitemap pages found."
            lines = [f"Sitemap: {cfg.get('name', client_key)} ({len(entries)} pages)\n"]
            for e in entries:
                pp = e["properties"]
                title = (_title(pp.get("Page Title", {}))
                         or _title(pp.get("Name", {}))
                         or "Untitled")
                parent = _text(pp.get("Parent Page", {}))
                slug = _text(pp.get("Slug", {}))
                page_type = _select(pp.get("Page Type", {}))
                raw_sections = _text(pp.get("Key Sections", {}))
                sections = ", ".join(
                    l.strip().lstrip("•–- ").strip()
                    for l in raw_sections.split("\n") if l.strip()
                )
                parent_str = f" › {parent}" if parent else ""
                type_str = f" [{page_type}]" if page_type else ""
                sec_str = f"\n    {sections}" if sections else ""
                lines.append(f"• {title}{parent_str}{type_str} /{slug}{sec_str}")
            return "\n".join(lines)

        elif name == "get_page_content":
            client_key = tool_input["client_key"]
            page_filter = tool_input.get("page_name", "").strip().lower()
            if client_key not in CLIENTS:
                return f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}"
            cfg = CLIENTS[client_key]
            entries = await notion.query_database(cfg["content_db_id"])
            results = []
            for e in entries:
                pp = e["properties"]
                page_name = (
                    _title(pp.get("Page Name", {}))
                    or _title(pp.get("Name", {}))
                )
                if page_filter and page_filter not in page_name.lower():
                    continue
                title_tag = _text(pp.get("Title Tag", {}))
                meta = _text(pp.get("Meta Description", {}))
                h1 = _text(pp.get("H1", {}))
                body = _text(pp.get("Body Copy", {}))
                body_preview = body[:400] + ("..." if len(body) > 400 else "")
                results.append(
                    f"PAGE: {page_name}\n"
                    f"  Title tag: {title_tag}\n"
                    f"  Meta: {meta}\n"
                    f"  H1: {h1}\n"
                    f"  Body: {body_preview}"
                )
            if not results:
                msg = f"No content found"
                if page_filter:
                    msg += f" for page matching '{page_filter}'"
                return msg + "."
            return "\n\n".join(results[:5])  # cap at 5 pages to keep response manageable

        elif name == "get_action_items":
            client_key = tool_input["client_key"]
            assignee = tool_input.get("assignee", "").strip()
            if client_key not in CLIENTS:
                return f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}"
            cfg = CLIENTS[client_key]
            filter_payload = None
            if assignee:
                filter_payload = {
                    "property": "Assigned To",
                    "select": {"equals": assignee},
                }
            entries = await notion.query_database(
                cfg["action_items_db_id"],
                filter_payload=filter_payload,
            )
            if not entries:
                return f"No action items found{' for ' + assignee if assignee else ''}."
            lines = []
            for e in entries:
                pp = e["properties"]
                task = _title(pp.get("Task", {})) or _title(pp.get("Name", {}))
                assigned = _select(pp.get("Assigned To", {}))
                status_val = _select(pp.get("Status", {}))
                due_obj = pp.get("Due Date", {}).get("date") or {}
                due = due_obj.get("start", "no due date")
                lines.append(f"• {task} [{assigned}] — {status_val} (due: {due})")
            return "\n".join(lines)

        else:
            return f"Unknown tool: {name}"

    except Exception as exc:
        return f"Error running tool '{name}': {exc}"


# ── Claude tool-use loop ──────────────────────────────────────────────────────

async def ask_rex(user_message: str) -> str:
    messages: list[dict] = [{"role": "user", "content": user_message}]

    for _ in range(6):  # max 6 rounds of tool use
        response = await claude.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1000,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            messages=messages,
        )

        if response.stop_reason == "end_turn":
            for block in response.content:
                if hasattr(block, "text"):
                    return block.text
            return "I ran out of things to say — try rephrasing?"

        elif response.stop_reason == "tool_use":
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []
            for block in response.content:
                if block.type == "tool_use":
                    result = await _execute_tool(block.name, block.input)
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result,
                    })
            messages.append({"role": "user", "content": tool_results})

        else:
            break

    return "Something went wrong — please try again."


# ── Slack event handlers ──────────────────────────────────────────────────────

async def _process(event: dict, say) -> None:
    # Strip @Rex mention from the text so Claude doesn't see it
    text = re.sub(r"<@\w+>", "", event.get("text", "")).strip()
    if not text:
        return
    reply = await ask_rex(text)
    await say(text=reply, thread_ts=event.get("ts"))


@slack_app.event("app_mention")
async def handle_mention(event, say):
    await _process(event, say)


@slack_app.event("message")
async def handle_message(event, say):
    # Only handle direct messages; ignore bot messages and message edits
    if (
        event.get("channel_type") == "im"
        and not event.get("bot_id")
        and not event.get("subtype")
    ):
        await _process(event, say)


# ── FastAPI app ───────────────────────────────────────────────────────────────

api = FastAPI(title="Rex — RxMedia AI Agent")
handler = AsyncSlackRequestHandler(slack_app)


@api.post("/slack/events")
async def slack_events(req: Request):
    return await handler.handle(req)


@api.get("/health")
async def health():
    return {"status": "ok", "agent": "Rex", "version": "1.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(api, host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
