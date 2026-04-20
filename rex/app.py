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

Tool modules:
  rex/tools/notion_tools.py   — Notion data lookups (pipeline, sitemap, content, SEO)
  rex/tools/clickup_tools.py  — ClickUp workspace, members, task creation
  rex/tools/pipeline_tools.py — Background stage runner (keyword research, GBP posts, etc.)
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
from src.integrations.notion import NotionClient
from rex.tools import (
    execute_notion_tool, NOTION_TOOL_NAMES,
    execute_clickup_tool, CLICKUP_TOOL_NAMES,
    execute_pipeline_tool, PIPELINE_TOOL_NAMES,
    execute_meeting_tool, MEETING_TOOL_NAMES,
    execute_email_tool, EMAIL_TOOL_NAMES,
    STAGE_COMMANDS, STAGE_LABELS,
)


# ── Initialize clients (read directly from os.environ — no pydantic Settings) ─

slack_app = AsyncApp(
    token=os.environ["SLACK_BOT_TOKEN"].strip(),
    signing_secret=os.environ["SLACK_SIGNING_SECRET"].strip(),
)
claude = anthropic.AsyncAnthropic(api_key=os.environ["ANTHROPIC_API_KEY"].strip())
notion = NotionClient(os.environ["NOTION_API_KEY"].strip())

# Current event context — set before each tool loop so background tasks can post back
_event_context: dict = {}


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

━━ UPDATING CLICKUP TASKS ━━
When asked to update a task ("mark X as done", "push the Y meeting to Tuesday", "change priority on Z"):
1. If the user gives you a task ID, use it directly. Otherwise use search_clickup_tasks to find the task by keyword.
2. If multiple tasks match, list them and ask which one.
3. Call update_clickup_task with only the fields being changed. Status values: "complete", "in progress", "to do", "waiting on client". Priority: 1=urgent 2=high 3=normal 4=low.
4. Confirm the change after updating ("✓ Marked 'Send COI' as complete").

━━ FLAGS (what needs attention) ━━
Flags live in a workspace-level Notion DB and track blockers, open actions, strategic signals, scope changes, and promises. They surface in the morning briefing under "Needs Attention."

When asked to close/resolve a flag ("we already handled WWMP leads", "the PDX Plumber COI is done", "close that flag"):
1. Call list_flags with the client_key if known, otherwise search by keyword in the results.
2. If multiple match, list them and ask which one.
3. Call resolve_flag with the flag_id. Default status is "Resolved". If the user said it's in progress, pass status="In Progress".
4. Confirm: "✓ Closed the WWMP leads flag."

When asked "what's flagged for X" or "what do I need to handle": call list_flags with the relevant filters. Default status is "Open".

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
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "get_client_services",
        "description": "Get the active services for a client — what they're paying for (care plan, SEO, blog, social, etc.) and how many blog posts per month.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
            },
            "required": ["client_key"],
        },
    },
    {
        "name": "get_pipeline_status",
        "description": "Get the current pipeline stage and status for a client from Notion.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
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
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
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
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
                "page_name": {"type": "string", "description": "Optional: filter to a specific page, e.g. 'Home', 'About Us'"},
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
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
                "assignee": {"type": "string", "description": "Optional: 'Agency' or 'Client' to filter by assignee"},
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
        "name": "update_clickup_task",
        "description": (
            "Update an existing ClickUp task. Use this to change status (e.g. 'complete', 'in progress'), "
            "push due dates, reassign, update priority, or edit name/description. "
            "If the user asks to 'mark done', 'close', or 'complete' a task, use status='complete'. "
            "If they want to change priority, priority is 1=urgent, 2=high, 3=normal, 4=low. "
            "If they ask to 'find' a task first, use search_clickup_tasks to get the task_id."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string", "description": "The ClickUp task ID to update"},
                "status": {"type": "string", "description": "New status (e.g. 'complete', 'in progress', 'waiting on client')"},
                "name": {"type": "string", "description": "Rename the task"},
                "description": {"type": "string", "description": "Update task description"},
                "due_date_ms": {"type": "integer", "description": "New due date as Unix timestamp in milliseconds"},
                "start_date_ms": {"type": "integer", "description": "New start date as Unix timestamp in milliseconds"},
                "priority": {"type": "integer", "description": "1=urgent, 2=high, 3=normal, 4=low"},
                "assignees_add": {"type": "array", "items": {"type": "integer"}, "description": "ClickUp user IDs to add as assignees"},
                "assignees_remove": {"type": "array", "items": {"type": "integer"}, "description": "ClickUp user IDs to remove from assignees"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "search_clickup_tasks",
        "description": "Search all ClickUp tasks by keyword. Returns task IDs, status, assignees, due dates. Use this to find a task before updating it.",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Keyword to search task names/descriptions for"},
                "include_closed": {"type": "boolean", "description": "Include completed tasks (default false)"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_care_plan_status",
        "description": "Get the latest care plan report for a client — PageSpeed scores, ADA status, privacy policy status, hours used this month.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
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
                "include_closed": {"type": "boolean", "description": "Whether to include completed/closed tasks. Default false."},
                "overdue_only": {"type": "boolean", "description": "If true, only return tasks past their due date."},
            },
            "required": [],
        },
    },
    # ── Meeting processing tools ──────────────────────────────────────────────
    {
        "name": "process_meeting",
        "description": "Process a meeting transcript into structured meeting notes, ClickUp tasks, and a follow-up email draft. The transcript should already exist as a Notion page (from Notion AI note taker). Returns a summary for Slack with an email draft awaiting approval.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
                "meeting_ref": {"type": "string", "description": "How to find the meeting: 'today', 'yesterday', or a Notion page ID. Default 'today'."},
            },
            "required": ["client_key"],
        },
    },
    {
        "name": "list_unprocessed_meetings",
        "description": "List meetings in the Client Log that haven't been processed yet.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "The client identifier, e.g. 'summit_therapy'"},
            },
            "required": ["client_key"],
        },
    },
    # ── Flag tools ─────────────────────────────────────────────────────────────
    {
        "name": "list_flags",
        "description": "List flags (blockers, open actions, strategic signals, scope changes) from the workspace Flags DB. Use this when the user asks 'what needs attention' or 'what flags are open for X'. Each result includes a Notion page id you can pass to resolve_flag.",
        "input_schema": {
            "type": "object",
            "properties": {
                "client_key": {"type": "string", "description": "Optional: limit to one client (e.g. 'wellness_works_management_partners')"},
                "status": {"type": "string", "description": "Filter by Status. Default 'Open'. Pass 'all' to include Open + In Progress.", "enum": ["Open", "In Progress", "Resolved", "Won't Fix", "all"]},
                "type": {"type": "string", "description": "Optional: filter by Type", "enum": ["BLOCKER", "OPEN_ACTION", "STRATEGIC", "RULE_SET", "PROMISE_MADE", "SCOPE_CHANGE"]},
            },
            "required": [],
        },
    },
    {
        "name": "resolve_flag",
        "description": "Mark a flag as Resolved (or Won't Fix / In Progress) in the Flags DB. Use when the user says something like 'close the WWMP leads flag' or 'we handled that already'. Call list_flags first to get the flag_id.",
        "input_schema": {
            "type": "object",
            "properties": {
                "flag_id": {"type": "string", "description": "The Notion page id of the flag (from list_flags output)"},
                "status": {"type": "string", "description": "New status. Default 'Resolved'.", "enum": ["Resolved", "Won't Fix", "In Progress", "Open"]},
                "notes": {"type": "string", "description": "Optional resolution notes"},
            },
            "required": ["flag_id"],
        },
    },
    # ── Email tools ───────────────────────────────────────────────────────────
    {
        "name": "send_follow_up_email",
        "description": "Send a follow-up email via Gmail on behalf of Keegan. Only use this after the user has approved an email draft (e.g. via thumbs-up reaction). Always CC keegan@rxmedia.io.",
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Full email body (plain text)"},
                "cc": {"type": "string", "description": "CC email address(es), comma-separated"},
            },
            "required": ["to", "subject", "body"],
        },
    },
]


# ── Tool dispatch ─────────────────────────────────────────────────────────────

async def _execute_tool(name: str, tool_input: dict) -> str:
    try:
        if name in NOTION_TOOL_NAMES:
            return await execute_notion_tool(name, tool_input, CLIENTS, notion)
        elif name in CLICKUP_TOOL_NAMES:
            return await execute_clickup_tool(name, tool_input)
        elif name in PIPELINE_TOOL_NAMES:
            return await execute_pipeline_tool(
                name, tool_input, CLIENTS, _event_context, slack_app.client
            )
        elif name in MEETING_TOOL_NAMES:
            return await execute_meeting_tool(name, tool_input, CLIENTS, notion._client)
        elif name in EMAIL_TOOL_NAMES:
            return await execute_email_tool(name, tool_input)
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
    channel   = event.get("channel")

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
    reply    = await ask_rex(messages)
    kwargs   = {"channel": event["channel"], "text": reply}
    if thread:
        kwargs["thread_ts"] = event.get("ts")
    await client.chat_postMessage(**kwargs)


# ── Pending email drafts (in-memory, keyed by message timestamp) ──────────────
# When Rex posts an email draft to Slack, we store it here.
# When Keegan reacts with :thumbsup:, we look it up and send.
_pending_emails: dict[str, dict] = {}
# Format: { "message_ts": { "to": "...", "subject": "...", "body": "...", "cc": "...", "channel": "..." } }


def store_pending_email(message_ts: str, email_data: dict) -> None:
    """Store an email draft awaiting Slack approval."""
    _pending_emails[message_ts] = email_data


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


@slack_app.event("reaction_added")
async def handle_reaction(event, client):
    """
    Handle thumbs-up reactions on email drafts.
    When Keegan reacts with :thumbsup: or :+1: to Rex's email draft message,
    Rex sends the email via Gmail and confirms in the thread.
    """
    reaction = event.get("reaction", "")
    if reaction not in ("+1", "thumbsup"):
        return

    message_ts = event.get("item", {}).get("ts", "")
    channel    = event.get("item", {}).get("channel", "")

    email_data = _pending_emails.pop(message_ts, None)
    if not email_data:
        return  # Not one of our pending emails

    try:
        from rex.tools.email_tools import send_email

        result = await send_email(
            to=email_data["to"],
            subject=email_data["subject"],
            body=email_data["body"],
            cc=email_data.get("cc", ""),
        )

        if "error" in result:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=message_ts,
                text=f"Failed to send email: {result['error']}",
            )
        else:
            await client.chat_postMessage(
                channel=channel,
                thread_ts=message_ts,
                text=f"Email sent to {email_data['to']}.",
            )
    except Exception as exc:
        await client.chat_postMessage(
            channel=channel,
            thread_ts=message_ts,
            text=f"Error sending email: {exc}",
        )


# ── FastAPI app ───────────────────────────────────────────────────────────────

api = FastAPI(title="Rex — RxMedia AI Agent")
handler = AsyncSlackRequestHandler(slack_app)


@api.post("/slack/events")
async def slack_events(req: Request):
    return await handler.handle(req)


@api.get("/health")
async def health():
    return {"status": "ok", "agent": "Rex", "version": "2.0"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(api, host="0.0.0.0", port=int(os.environ.get("PORT", 3000)))
