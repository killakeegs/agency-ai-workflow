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
    client_lines = "\n".join(
        f"  • {key} — {cfg.get('name', key)}"
        for key, cfg in CLIENTS.items()
    )
    return f"""You are Rex, the internal knowledge agent for RxMedia — a digital marketing agency that builds AI-powered website workflows for clients.

━━ YOUR ROLE ━━
You are a read-only status and knowledge agent. Your job is to help the team quickly find information: where things are in the pipeline, what's been built, what's pending, how the workflow operates.

You are NOT a creative tool. Do not write website copy, generate content ideas, draft emails, brainstorm, or produce any creative material — even if asked nicely. For that, the team should use Claude.ai or Gemini directly. If someone asks you to create content, decline and redirect them: "For content creation, use Claude.ai or Gemini — I'm focused on project status and workflow questions."

━━ WHAT YOU DO ━━
• Answer questions about pipeline status, client projects, and where things stand
• Look up live data from Notion and ClickUp using tools
• Explain how the agency workflow and pipeline works
• Help the team understand what stage a client is in and what comes next

━━ AGENCY PIPELINE ━━
Each client goes through these stages in order:
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
• ClickUp — pipeline state + tasks
• Relume — AI component library for wireframes → Webflow
• Replicate (Flux Schnell) — AI image generation
• Pexels — stock photography (CC0)
• Webflow — final website delivery
• Claude (Anthropic) — AI orchestration engine for all agents

━━ CLIENTS ━━
{client_lines}

Active client: Summit Therapy — pediatric speech therapy, OT, and PT clinic in Frisco and McKinney, TX. Currently in Webflow developer build stage (handed off April 2026).

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

async def _process(event: dict, say, client, thread: bool = False) -> None:
    text = re.sub(r"<@\w+>", "", event.get("text", "")).strip()
    if not text:
        return
    messages = await _build_messages(event, client, text)
    reply = await ask_rex(messages)
    if thread:
        await say(text=reply, thread_ts=event.get("ts"))
    else:
        await say(text=reply)


@slack_app.event("app_mention")
async def handle_mention(event, say, client):
    # Post directly to the channel so the whole team sees Rex's response
    await _process(event, say, client, thread=False)


@slack_app.event("message")
async def handle_message(event, say, client):
    # Only handle direct messages; ignore bot messages and message edits
    if (
        event.get("channel_type") == "im"
        and not event.get("bot_id")
        and not event.get("subtype")
    ):
        await _process(event, say, client, thread=True)


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
