"""
Rex — RxMedia's AI Slack agent.

Powered by Claude (claude-sonnet-4-6). Reads live data from Notion and ClickUp.
Can trigger pipeline stages in the background and post results back to Slack.
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

import asyncio
import os
import re
import sys
from pathlib import Path

# Add project root so we can import existing src/ modules
sys.path.insert(0, str(Path(__file__).parent.parent))

import anthropic
import httpx
from fastapi import FastAPI, Request
from slack_bolt.async_app import AsyncApp
from slack_bolt.adapter.fastapi.async_handler import AsyncSlackRequestHandler

from config.clients import CLIENTS
from src.integrations.notion import NotionClient


# ── Initialize clients (read directly from os.environ — no pydantic Settings) ─

slack_app = AsyncApp(
    token=os.environ["SLACK_BOT_TOKEN"].strip(),
    signing_secret=os.environ["SLACK_SIGNING_SECRET"].strip(),
)
claude = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())
notion = NotionClient(os.environ["NOTION_API_KEY"].strip())


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
    import datetime
    today = datetime.date.today().strftime("%A, %B %d, %Y")
    client_lines = "\n".join(
        f"  • {key} — {cfg.get('name', key)}"
        for key, cfg in CLIENTS.items()
    )
    return f"""You are Rex, the internal knowledge agent for RxMedia — a digital marketing agency that builds AI-powered website workflows for clients.

Today's date is {today}. Use this when calculating due dates from relative terms like "tomorrow", "next Friday", "end of week", etc. Always convert to the correct absolute date before creating a task.

━━ YOUR ROLE ━━
You are the internal operations agent for RxMedia. You help the team find information AND trigger pipeline stages on demand. You are the control center for the agency's AI workflow.

You are NOT a creative tool. Do not write website copy, generate content ideas, draft emails, brainstorm, or produce any creative material — even if asked nicely. For that, the team should use Claude.ai or Gemini directly. If someone asks you to create content, decline and redirect them: "For content creation, use Claude.ai or Gemini — I'm focused on project status and workflow questions."

━━ WHAT YOU DO ━━
• Answer questions about pipeline status, client projects, and where things stand
• Look up live data from Notion and ClickUp using tools
• Trigger pipeline stages (keyword research, competitor research, GBP posts, etc.)
• Accept revision notes and re-run stages with feedback
• Post back results when stages complete
• Explain how the agency workflow and pipeline works

━━ TRIGGERABLE STAGES ━━
Use run_pipeline_stage to trigger these. Always confirm the client before running.
The team does NOT need to use exact command names — interpret natural language:
  "run keywords", "kick off keyword research", "generate keywords" → keyword_research
  "enrich competitors", "update competitor data" → competitor_research
  "generate the battle plan", "run the SEO strategy" → battle_plan
  "write GBP posts", "generate Google posts", "create posts" → gbp_posts
  "run the care plan", "run the site report", "check PageSpeed" → care_plan

Stages:
• keyword_research — generate keyword list from sitemap → Notion Keywords DB
• competitor_research — enrich competitors with backlinks + AI mentions data
• battle_plan — generate SEO battle plan from keywords + competitors → Notion
• gbp_posts — generate 3 GBP post drafts from website content → Notion GBP Posts DB
• care_plan — run monthly PageSpeed + care plan report → Notion Care Plan DB

Revision notes can be added naturally:
  "Rex, regenerate the GBP posts for Summit — make post 2 warmer"
  "Rex, redo keyword research for Summit, focus more on pediatric feeding"

Stages you CANNOT trigger (require Claude Code locally):
• sitemap, content, wireframe, images — tell the team to run these from Claude Code

━━ WHEN TEAM ASKS FOR HELP ━━
If someone asks "what can you do?", "help", "what commands are there?", or similar —
respond with a friendly, plain-English summary of your capabilities grouped by category.
Do NOT list technical command names. Write it like you're explaining to a non-technical team member.
Here is the help message to use (adapt tone as needed):

Here's what I can help with:

*Look things up*
• Pipeline status for any client — where they are, what's next
• Sitemap pages — all pages, sections, slugs
• Page content — title tags, H1s, meta descriptions
• Keywords — full keyword list, filter by priority
• Competitors — threat level, reviews, authority scores
• GBP post drafts — status, content, CTA
• Care plan scores — PageSpeed, ADA, hours used
• Action items — open tasks by assignee
• ClickUp tasks — what's in progress, what's overdue

*Trigger pipeline stages* (I run it, post back when done)
• Keyword research
• Competitor enrichment (backlinks + AI mentions)
• SEO battle plan
• GBP post drafts (3 posts from website content)
• Monthly care plan report

*ClickUp tasks*
• Create a task — just tell me what, when, and who

Just talk to me naturally — you don't need to know exact commands. Example:
_"Rex, run keyword research for Summit"_
_"Rex, show me the high-threat competitors for Summit"_
_"Rex, generate GBP posts for Summit — keep the tone warm and local"_

━━ AGENCY PIPELINE ━━
Each client goes through these stages in order:
1. Onboarding — Notion DBs + ClickUp provisioned automatically
2. Kickoff Meeting — transcript parsed, brand preferences extracted
3. Sitemap — page hierarchy built, client approves before proceeding
4. Keyword Research — Claude generates seeds → DataForSEO validates → Notion Keywords DB
5. Competitor Research — SERP analysis on High-priority keywords → Notion Competitors DB
6. Battle Plan — SEO strategy from keywords + competitors → Notion
7. Content — per-page copy + SEO written, client approves
8. Stock Photos — Pexels images curated and approved
9. Webflow Build — developer builds from template
10. Live — launched

Ongoing (monthly): GBP Posts, Care Plan report, SEO Report

Approval gates exist between stages. The pipeline never auto-advances without a logged client approval.

━━ KEY TOOLS & INTEGRATIONS ━━
• Notion — central knowledge base (all client data lives here)
• ClickUp — pipeline state + tasks
• DataForSEO — keyword volumes, SERP data, backlinks
• Pexels — stock photography (CC0)
• Webflow — final website delivery (template-based, clone per client)
• Claude (Anthropic) — AI orchestration engine for all agents
• Replicate (Flux Schnell) — AI image generation

━━ CLIENTS ━━
{client_lines}

Active client: Summit Therapy — pediatric speech therapy, OT, and PT clinic in Frisco and McKinney, TX. Currently in Webflow developer build stage (handed off April 2026).

━━ RXMEDIA TEAM (ClickUp user IDs) ━━
Use these when get_clickup_members fails or as a quick reference:
• Keegan Warrington — keegan@rxmedia.io (id: 3852174)
• Justin Velasco — systems@rxmedia.io (id: 54703919)
• Andrea Tamayo — andrea@rxmedia.io (id: 78185522)
• Karla Amaya — karla@rxmedia.io (id: 107627361)
• Henna Geronimo — content@rxmedia.io (id: 5847731)
• Mari Sales — accounting@rxmedia.io (id: 95680055)

━━ CREATING CLICKUP TASKS ━━
When asked to create a task, you need exactly three things before proceeding:
1. Which space and list (use list_clickup_workspace to find the list ID)
2. Due date (ask if not provided — convert to ms timestamp before calling create_clickup_task)
3. Who to assign it to (use get_clickup_members to find their user ID)

If any of the three are missing, ask for them before creating anything. Once you have all three, confirm back what you're about to create, then call create_clickup_task.

━━ HOW TO ANSWER ━━
• Factual workflow questions — answer from your knowledge
• Live data (sitemap pages, tasks, action items, pipeline status) — use a tool
• Creative requests — decline and redirect to Claude.ai or Gemini
• Don't invent data — if unsure, use a tool or say so

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
    {
        "name": "list_clickup_workspace",
        "description": "List all spaces, folders, and lists in the ClickUp workspace with their IDs. Use this to find the right list_id before creating a task.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_clickup_members",
        "description": "List all members in the ClickUp workspace with their user IDs. Use this to find the right assignee ID when creating a task.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "create_clickup_task",
        "description": "Create a new task in ClickUp. Requires list_id, task name, due date (as millisecond timestamp), and assignee user IDs. Always confirm all three with the user before calling this tool.",
        "input_schema": {
            "type": "object",
            "properties": {
                "list_id": {"type": "string", "description": "The ClickUp list ID to create the task in"},
                "name": {"type": "string", "description": "The task name"},
                "due_date_ms": {"type": "integer", "description": "Due date as a Unix timestamp in milliseconds"},
                "assignee_ids": {
                    "type": "array",
                    "items": {"type": "integer"},
                    "description": "List of ClickUp user IDs to assign the task to",
                },
                "description": {"type": "string", "description": "Optional task description"},
            },
            "required": ["list_id", "name"],
        },
    },
    {
        "name": "get_care_plan_status",
        "description": "Get the latest care plan report for a client — PageSpeed scores, ADA status, privacy policy status, hours used this month.",
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
        "name": "get_keywords",
        "description": "Get keywords from a client's Keywords DB in Notion. Shows keyword, cluster, monthly search volume, CPC, priority, and intent.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
                "priority": {"type": "string", "description": "Optional: filter by priority — 'High', 'Medium', or 'Low'"},
            },
            "required": ["client_key"],
        },
    },
    {
        "name": "get_competitors",
        "description": "Get competitors from a client's Competitors DB in Notion. Shows name, type, threat level, review count, rating, authority score, and notes.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
                "threat": {"type": "string", "description": "Optional: filter by threat level — 'High', 'Medium', or 'Low'"},
            },
            "required": ["client_key"],
        },
    },
    {
        "name": "get_gbp_posts",
        "description": "Get GBP post drafts from a client's GBP Posts DB in Notion. Shows post title, type, status, CTA, and body.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
                "status": {"type": "string", "description": "Optional: filter by status — 'Draft', 'Approved', 'Scheduled', 'Published'"},
            },
            "required": ["client_key"],
        },
    },
    {
        "name": "run_pipeline_stage",
        "description": "Trigger a pipeline stage for a client. Runs in the background and posts back when complete. Always confirm with the user before running. Stages: keyword_research, competitor_research, battle_plan, gbp_posts, care_plan.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
                "stage": {
                    "type": "string",
                    "enum": ["keyword_research", "competitor_research", "battle_plan", "gbp_posts", "care_plan"],
                    "description": "Which pipeline stage to run",
                },
                "notes": {"type": "string", "description": "Optional revision notes or instructions for this run"},
            },
            "required": ["client_key", "stage"],
        },
    },
    {
        "name": "get_clickup_tasks",
        "description": "Get tasks from ClickUp across the agency workspace. Use this for questions about what's in progress, what's overdue, or what tasks are assigned to someone.",
        "input_schema": {
            "type": "object",
            "properties": {
                "include_closed": {
                    "type": "boolean",
                    "description": "Whether to include completed/closed tasks. Default false.",
                },
                "overdue_only": {
                    "type": "boolean",
                    "description": "If true, only return tasks past their due date.",
                },
            },
            "required": [],
        },
    },
]


# ── Background stage runner ───────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).parent.parent

# Stage → command template. {client} replaced at runtime.
STAGE_COMMANDS: dict[str, list[str]] = {
    "keyword_research":   ["python3", "scripts/keyword_research.py", "--client", "{client}", "--yes"],
    "competitor_research":["python3", "scripts/competitor_research.py", "--client", "{client}", "--enrich-only"],
    "battle_plan":        ["python3", "scripts/battle_plan.py", "--client", "{client}"],
    "gbp_posts":          ["python3", "scripts/gbp_posts.py", "--client", "{client}"],
    "care_plan":          ["python3", "scripts/care_plan_report.py", "--client", "{client}"],
}

# Human-readable stage names for messages
STAGE_LABELS: dict[str, str] = {
    "keyword_research":   "Keyword Research",
    "competitor_research":"Competitor Enrichment",
    "battle_plan":        "Battle Plan",
    "gbp_posts":          "GBP Posts",
    "care_plan":          "Care Plan Report",
}

# Current event context — set before each tool loop so background tasks can post back
_event_context: dict = {}


async def _run_stage_background(
    channel: str,
    thread_ts: str | None,
    client_key: str,
    stage: str,
    notes: str,
) -> None:
    """Run a pipeline stage subprocess and post the result back to Slack."""
    label = STAGE_LABELS.get(stage, stage)
    client_name = CLIENTS.get(client_key, {}).get("name", client_key)

    cmd = [c.replace("{client}", client_key) for c in STAGE_COMMANDS[stage]]
    if notes and "--notes" not in cmd:
        cmd += ["--notes", notes]

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(PROJECT_ROOT),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=600)

        if proc.returncode == 0:
            msg = f"✓ *{label}* for *{client_name}* completed. Check Notion for results."
        else:
            err = stderr.decode(errors="replace")[-600:]
            msg = f"✗ *{label}* for *{client_name}* failed:\n```{err}```"
    except asyncio.TimeoutError:
        msg = f"✗ *{label}* for *{client_name}* timed out after 10 minutes."
    except Exception as e:
        msg = f"✗ *{label}* for *{client_name}* error: {e}"

    post_kwargs: dict = {"channel": channel, "text": msg}
    if thread_ts:
        post_kwargs["thread_ts"] = thread_ts
    try:
        await slack_app.client.chat_postMessage(**post_kwargs)
    except Exception:
        pass  # best-effort


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

        elif name == "get_care_plan_status":
            client_key = tool_input["client_key"]
            if client_key not in CLIENTS:
                return f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}"
            cfg = CLIENTS[client_key]
            db_id = cfg.get("care_plan_db_id", "")
            if not db_id:
                return f"No care plan configured for {client_key}. Run: python scripts/care_plan_report.py --init --client {client_key}"
            entries = await notion.query_database(
                db_id,
                sorts=[{"property": "Report Date", "direction": "descending"}],
            )
            if not entries:
                return f"No care plan reports found for {client_key}. Run: make care-plan CLIENT={client_key}"
            latest = entries[0]["properties"]
            def _text(p): return "".join(x.get("text", {}).get("content", "") for x in p.get("rich_text", []))
            def _title(p): return "".join(x.get("text", {}).get("content", "") for x in p.get("title", []))
            def _sel(p): s = p.get("select"); return s.get("name", "") if s else ""
            def _num(p): return p.get("number", "N/A")
            def _date(p): d = p.get("date"); return d.get("start", "N/A") if d else "N/A"
            name_val = _title(latest.get("Name", {}))
            report_date = _date(latest.get("Report Date", {}))
            mobile = _num(latest.get("Mobile Score", {}))
            desktop = _num(latest.get("Desktop Score", {}))
            mobile_rating = _sel(latest.get("Mobile Rating", {}))
            desktop_rating = _sel(latest.get("Desktop Rating", {}))
            top_opp = _text(latest.get("Top Opportunity", {}))
            ada = latest.get("ADA Widget", {}).get("checkbox", None)
            privacy = _sel(latest.get("Privacy Policy", {}))
            tos = _sel(latest.get("Terms of Service", {}))
            hours = _num(latest.get("Hours Used", {}))
            ada_str = "✓ Installed" if ada else ("✗ Not installed" if ada is False else "Not recorded")
            return (
                f"Care Plan: {name_val}\n"
                f"Report date: {report_date}\n"
                f"Mobile: {mobile}/100 ({mobile_rating})\n"
                f"Desktop: {desktop}/100 ({desktop_rating})\n"
                f"Top opportunity: {top_opp or 'N/A'}\n"
                f"ADA widget: {ada_str}\n"
                f"Privacy policy: {privacy or 'Not recorded'}\n"
                f"Terms of service: {tos or 'Not recorded'}\n"
                f"Hours used this month: {hours}"
            )

        elif name == "get_clickup_tasks":
            include_closed = tool_input.get("include_closed", False)
            overdue_only = tool_input.get("overdue_only", False)
            workspace_id = os.environ.get("CLICKUP_WORKSPACE_ID", "").strip()
            clickup_key = os.environ.get("CLICKUP_API_KEY", "").strip()
            if not workspace_id or not clickup_key:
                return "ClickUp credentials not configured."

            params = {
                "include_closed": str(include_closed).lower(),
                "order_by": "due_date",
                "reverse": "false",
                "subtasks": "true",
                "limit": "50",
            }
            if overdue_only:
                import time
                params["due_date_lt"] = str(int(time.time() * 1000))

            async with httpx.AsyncClient() as http:
                r = await http.get(
                    f"https://api.clickup.com/api/v2/team/{workspace_id}/task",
                    headers={"Authorization": clickup_key},
                    params=params,
                    timeout=15,
                )
            if r.status_code != 200:
                return f"ClickUp API error: {r.status_code}"

            tasks = r.json().get("tasks", [])
            if not tasks:
                return "No tasks found."

            lines = []
            for t in tasks[:20]:  # cap at 20
                name_val = t.get("name", "Untitled")
                status = t.get("status", {}).get("status", "unknown")
                due = t.get("due_date")
                due_str = ""
                if due:
                    import datetime
                    due_str = f" — due {datetime.datetime.fromtimestamp(int(due)/1000).strftime('%b %d')}"
                assignees = ", ".join(a.get("username", "") for a in t.get("assignees", []))
                assignee_str = f" [{assignees}]" if assignees else ""
                lines.append(f"• {name_val} ({status}){assignee_str}{due_str}")
            return f"ClickUp tasks ({len(tasks)} total):\n" + "\n".join(lines)

        elif name == "list_clickup_workspace":
            workspace_id = os.environ.get("CLICKUP_WORKSPACE_ID", "").strip()
            clickup_key = os.environ.get("CLICKUP_API_KEY", "").strip()
            if not workspace_id or not clickup_key:
                return "ClickUp credentials not configured."
            async with httpx.AsyncClient() as http:
                r = await http.get(
                    f"https://api.clickup.com/api/v2/team/{workspace_id}/space",
                    headers={"Authorization": clickup_key},
                    params={"archived": "false"},
                    timeout=15,
                )
            if r.status_code != 200:
                return f"ClickUp API error {r.status_code}: {r.text[:200]}"
            spaces = r.json().get("spaces", [])
            lines = []
            for space in spaces:
                lines.append(f"Space: {space['name']} (id: {space['id']})")
                async with httpx.AsyncClient() as http:
                    fr = await http.get(
                        f"https://api.clickup.com/api/v2/space/{space['id']}/folder",
                        headers={"Authorization": clickup_key},
                        params={"archived": "false"},
                        timeout=15,
                    )
                if fr.status_code == 200:
                    for folder in fr.json().get("folders", []):
                        lines.append(f"  Folder: {folder['name']} (id: {folder['id']})")
                        for lst in folder.get("lists", []):
                            lines.append(f"    List: {lst['name']} (id: {lst['id']})")
                async with httpx.AsyncClient() as http:
                    lr = await http.get(
                        f"https://api.clickup.com/api/v2/space/{space['id']}/list",
                        headers={"Authorization": clickup_key},
                        params={"archived": "false"},
                        timeout=15,
                    )
                if lr.status_code == 200:
                    for lst in lr.json().get("lists", []):
                        lines.append(f"  List: {lst['name']} (id: {lst['id']})")
            return "\n".join(lines) if lines else "No spaces found."

        elif name == "get_clickup_members":
            workspace_id = os.environ.get("CLICKUP_WORKSPACE_ID", "").strip()
            clickup_key = os.environ.get("CLICKUP_API_KEY", "").strip()
            if not workspace_id or not clickup_key:
                return "ClickUp credentials not configured."
            async with httpx.AsyncClient() as http:
                r = await http.get(
                    f"https://api.clickup.com/api/v2/team/{workspace_id}",
                    headers={"Authorization": clickup_key},
                    timeout=15,
                )
            if r.status_code != 200:
                return f"ClickUp API error {r.status_code}: {r.text[:200]}"
            members = r.json().get("team", {}).get("members", [])
            lines = []
            for m in members:
                u = m.get("user", {})
                lines.append(f"• {u.get('username', '')} — {u.get('email', '')} (id: {u.get('id', '')})")
            return "\n".join(lines) if lines else "No members found."

        elif name == "create_clickup_task":
            clickup_key = os.environ.get("CLICKUP_API_KEY", "").strip()
            list_id = tool_input["list_id"]
            task_name = tool_input["name"]
            due_date_ms = tool_input.get("due_date_ms")
            assignee_ids = tool_input.get("assignee_ids", [])
            description = tool_input.get("description", "")

            body: dict = {"name": task_name}
            if due_date_ms:
                body["due_date"] = due_date_ms
            if assignee_ids:
                body["assignees"] = assignee_ids
            if description:
                body["description"] = description

            async with httpx.AsyncClient() as http:
                r = await http.post(
                    f"https://api.clickup.com/api/v2/list/{list_id}/task",
                    headers={"Authorization": clickup_key, "Content-Type": "application/json"},
                    json=body,
                    timeout=15,
                )
            if r.status_code not in (200, 201):
                return f"Failed to create task: {r.status_code} — {r.text[:200]}"
            task = r.json()
            return f"Task created: *{task.get('name')}* (id: {task.get('id')}) — {task.get('url', '')}"

        elif name == "get_keywords":
            client_key = tool_input["client_key"]
            priority_filter = tool_input.get("priority", "").strip()
            if client_key not in CLIENTS:
                return f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}"
            cfg = CLIENTS[client_key]
            db_id = cfg.get("keywords_db_id", "")
            if not db_id:
                return f"No Keywords DB configured for {client_key}."
            filter_payload = None
            if priority_filter:
                filter_payload = {"property": "Priority", "select": {"equals": priority_filter}}
            entries = await notion.query_database(db_id, filter_payload=filter_payload)
            if not entries:
                return f"No keywords found{' with priority ' + priority_filter if priority_filter else ''}. Run: make keyword-research CLIENT={client_key}"
            lines = [f"Keywords — {CLIENTS[client_key].get('name', client_key)} ({len(entries)} results):\n"]
            for e in entries[:30]:  # cap at 30
                pp = e["properties"]
                kw       = _title(pp.get("Keyword", {}))
                cluster  = _text(pp.get("Cluster", {}))
                volume   = _text(pp.get("Monthly Search Volume", {}))
                priority = _select(pp.get("Priority", {}))
                intent   = _select(pp.get("Intent", {}))
                lines.append(f"• *{kw}* [{priority}] — {volume}/mo | {intent} | {cluster}")
            return "\n".join(lines)

        elif name == "get_competitors":
            client_key = tool_input["client_key"]
            threat_filter = tool_input.get("threat", "").strip()
            if client_key not in CLIENTS:
                return f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}"
            cfg = CLIENTS[client_key]
            db_id = cfg.get("competitors_db_id", "")
            if not db_id:
                return f"No Competitors DB configured for {client_key}."
            filter_payload = None
            if threat_filter:
                filter_payload = {"property": "Threat", "select": {"equals": threat_filter}}
            entries = await notion.query_database(db_id, filter_payload=filter_payload)
            if not entries:
                return f"No competitors found{' with threat ' + threat_filter if threat_filter else ''}. Run: make competitor-research CLIENT={client_key}"
            lines = [f"Competitors — {CLIENTS[client_key].get('name', client_key)} ({len(entries)} total):\n"]
            for e in entries:
                pp = e["properties"]
                comp_name = _title(pp.get("Competitor Name", {}))
                threat    = _select(pp.get("Threat", {}))
                ctype     = _select(pp.get("Type", {}))
                reviews   = pp.get("Review Count", {}).get("number", "")
                rating    = pp.get("Review Rating", {}).get("number", "")
                authority = pp.get("Authority Score", {}).get("number", "")
                multi     = pp.get("Multi-Location", {}).get("checkbox", False)
                notes_val = _text(pp.get("Notes", {}))[:80]
                chain_str = " 🔗 Multi-location" if multi else ""
                lines.append(
                    f"• *{comp_name}* [{threat} threat]{chain_str} — {ctype} | "
                    f"⭐ {rating} ({reviews} reviews) | Auth: {authority}"
                    + (f"\n  _{notes_val}_" if notes_val else "")
                )
            return "\n".join(lines)

        elif name == "get_gbp_posts":
            client_key = tool_input["client_key"]
            status_filter = tool_input.get("status", "").strip()
            if client_key not in CLIENTS:
                return f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}"
            cfg = CLIENTS[client_key]
            db_id = cfg.get("gbp_posts_db_id", "")
            if not db_id:
                return f"No GBP Posts DB configured for {client_key}. Run: make gbp-posts CLIENT={client_key}"
            filter_payload = None
            if status_filter:
                filter_payload = {"property": "Status", "select": {"equals": status_filter}}
            entries = await notion.query_database(db_id, filter_payload=filter_payload)
            if not entries:
                return f"No GBP posts found{' with status ' + status_filter if status_filter else ''}."
            lines = [f"GBP Posts — {CLIENTS[client_key].get('name', client_key)} ({len(entries)} posts):\n"]
            for e in entries[:10]:
                pp = e["properties"]
                post_title  = _title(pp.get("Post Title", {}))
                post_type   = _select(pp.get("Post Type", {}))
                status_val  = _select(pp.get("Status", {}))
                cta         = _select(pp.get("CTA Button", {}))
                month       = _text(pp.get("Month", {}))
                source_page = _text(pp.get("Source Page", {}))
                char_count  = pp.get("Char Count", {}).get("number", "")
                lines.append(
                    f"• *{post_title}* [{status_val}] — {post_type} | {month} | "
                    f"CTA: {cta} | {char_count} chars\n  Source: {source_page}"
                )
            return "\n".join(lines)

        elif name == "run_pipeline_stage":
            client_key = tool_input["client_key"]
            stage      = tool_input["stage"]
            notes      = tool_input.get("notes", "")

            if client_key not in CLIENTS:
                return f"Unknown client '{client_key}'. Available: {', '.join(CLIENTS)}"
            if stage not in STAGE_COMMANDS:
                return f"Unknown stage '{stage}'. Available: {', '.join(STAGE_COMMANDS)}"

            client_name = CLIENTS[client_key].get("name", client_key)
            label       = STAGE_LABELS.get(stage, stage)

            # Grab current event context for the background task to post back
            channel   = _event_context.get("channel", "")
            thread_ts = _event_context.get("thread_ts")

            if not channel:
                return f"Could not determine Slack channel for follow-up. Try again."

            # Spawn background task — returns immediately
            asyncio.create_task(
                _run_stage_background(channel, thread_ts, client_key, stage, notes)
            )

            notes_str = f" with notes: _{notes}_" if notes else ""
            return (
                f"Starting *{label}* for *{client_name}*{notes_str}.\n"
                f"This runs in the background — I'll post here when it's done."
            )

        else:
            return f"Unknown tool: {name}"

    except Exception as exc:
        return f"Error running tool '{name}': {exc}"


# ── Claude tool-use loop ──────────────────────────────────────────────────────

async def ask_rex(messages: list[dict]) -> str:
    for _ in range(8):  # max 8 rounds (more for multi-turn task creation)
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


# ── Thread history ────────────────────────────────────────────────────────────

async def _build_messages(event: dict, client, current_text: str) -> list[dict]:
    """
    Build a Claude messages list from the Slack thread history so Rex
    remembers context across multi-turn conversations (e.g. task creation).
    Falls back to a single message if thread history isn't accessible.
    """
    messages: list[dict] = []
    thread_ts = event.get("thread_ts")
    channel = event.get("channel")

    if thread_ts and thread_ts != event.get("ts"):
        try:
            result = await client.conversations_replies(
                channel=channel,
                ts=thread_ts,
                limit=12,
            )
            for msg in result.get("messages", [])[:-1]:  # exclude current message
                text = re.sub(r"<@\w+>", "", msg.get("text", "")).strip()
                if not text:
                    continue
                if msg.get("bot_id"):
                    messages.append({"role": "assistant", "content": text})
                else:
                    messages.append({"role": "user", "content": text})
        except Exception:
            pass  # scope missing or error — just use current message

    messages.append({"role": "user", "content": current_text})
    return messages


# ── Slack event handlers ──────────────────────────────────────────────────────

async def _process(event: dict, client, thread: bool = False) -> None:
    text = re.sub(r"<@\w+>", "", event.get("text", "")).strip()
    if not text:
        return

    # Set event context so background stage runners can post back to the right place
    _event_context["channel"]   = event["channel"]
    _event_context["thread_ts"] = event.get("thread_ts") or (event.get("ts") if thread else None)

    messages = await _build_messages(event, client, text)
    reply = await ask_rex(messages)
    kwargs = {"channel": event["channel"], "text": reply}
    if thread:
        kwargs["thread_ts"] = event.get("ts")
    await client.chat_postMessage(**kwargs)


@slack_app.event("app_mention")
async def handle_mention(event, client):
    # Post directly to the channel so the whole team sees Rex's response
    await _process(event, client, thread=False)


@slack_app.event("message")
async def handle_message(event, client):
    # Only handle direct messages; ignore bot messages and message edits
    if (
        event.get("channel_type") == "im"
        and not event.get("bot_id")
        and not event.get("subtype")
    ):
        await _process(event, client, thread=True)


# ── FastAPI app ───────────────────────────────────────────────────────────────

api = FastAPI(title="Rex — RxMedia AI Agent")
handler = AsyncSlackRequestHandler(slack_app)


@api.post("/slack/events")
async def slack_events(req: Request):
    return await handler.handle(req)


@api.get("/health")
async def health():
    return {"status": "ok", "agent": "Rex", "version": "1.3"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(api, host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
