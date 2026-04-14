"""
Pipeline stage runner for Rex.

Handles triggering background pipeline stages (keyword research, GBP posts, etc.)
via subprocess, then posting results back to Slack.
"""
from __future__ import annotations

import asyncio
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent.parent

# Stage → command template. {client} is replaced at runtime.
STAGE_COMMANDS: dict[str, list[str]] = {
    "keyword_research":    ["python3", "scripts/seo/keyword_research.py", "--client", "{client}", "--yes"],
    "competitor_research": ["python3", "scripts/seo/competitor_research.py", "--client", "{client}", "--enrich-only"],
    "battle_plan":         ["python3", "scripts/seo/battle_plan.py", "--client", "{client}"],
    "gbp_posts":           ["python3", "scripts/seo/gbp_posts.py", "--client", "{client}"],
    "care_plan":           ["python3", "scripts/care/care_plan_report.py", "--client", "{client}"],
    "blog_ideas":          ["python3", "scripts/blog/blog_ideas.py", "--client", "{client}"],
    "blog_write":          ["python3", "scripts/blog/blog_write.py", "--client", "{client}"],
}

# Human-readable labels used in Slack messages
STAGE_LABELS: dict[str, str] = {
    "keyword_research":    "Keyword Research",
    "competitor_research": "Competitor Enrichment",
    "battle_plan":         "Battle Plan",
    "gbp_posts":           "GBP Posts",
    "care_plan":           "Care Plan Report",
    "blog_ideas":          "Blog Ideas",
    "blog_write":          "Blog Write",
}

PIPELINE_TOOL_NAMES = {"run_pipeline_stage"}


async def run_stage_background(
    slack_client,
    channel: str,
    thread_ts: str | None,
    client_key: str,
    stage: str,
    notes: str,
    client_name: str,
) -> None:
    """
    Run a pipeline stage as a background subprocess and post the result to Slack.
    Called via asyncio.create_task — never awaited directly.
    """
    label = STAGE_LABELS.get(stage, stage)
    cmd   = [c.replace("{client}", client_key) for c in STAGE_COMMANDS[stage]]
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
        await slack_client.chat_postMessage(**post_kwargs)
    except Exception:
        pass  # best-effort


async def execute_pipeline_tool(
    name: str,
    tool_input: dict,
    clients: dict,
    event_context: dict,
    slack_client,
) -> str:
    """Dispatch a pipeline tool call."""

    if name == "run_pipeline_stage":
        client_key = tool_input["client_key"]
        stage      = tool_input["stage"]
        notes      = tool_input.get("notes", "")

        if client_key not in clients:
            return f"Unknown client '{client_key}'. Available: {', '.join(clients)}"
        if stage not in STAGE_COMMANDS:
            return f"Unknown stage '{stage}'. Available: {', '.join(STAGE_COMMANDS)}"

        client_name = clients[client_key].get("name", client_key)
        label       = STAGE_LABELS.get(stage, stage)
        channel     = event_context.get("channel", "")
        thread_ts   = event_context.get("thread_ts")

        if not channel:
            return "Could not determine Slack channel for follow-up. Try again."

        asyncio.create_task(
            run_stage_background(slack_client, channel, thread_ts, client_key, stage, notes, client_name)
        )

        notes_str = f" with notes: _{notes}_" if notes else ""
        return (
            f"Starting *{label}* for *{client_name}*{notes_str}.\n"
            f"This runs in the background — I'll post here when it's done."
        )

    return f"Unknown pipeline tool: {name}"
