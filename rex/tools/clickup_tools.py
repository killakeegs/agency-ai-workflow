"""
ClickUp tool handlers for Rex.

Handles workspace browsing, member lookup, task listing, and task creation
via the ClickUp REST API.
"""
from __future__ import annotations

import os
import time
import datetime

import httpx


CLICKUP_TOOL_NAMES = {
    "get_clickup_tasks",
    "list_clickup_workspace",
    "get_clickup_members",
    "create_clickup_task",
}


def _clickup_creds() -> tuple[str, str]:
    """Return (workspace_id, api_key) from environment, raising if missing."""
    workspace_id = os.environ.get("CLICKUP_WORKSPACE_ID", "").strip()
    api_key      = os.environ.get("CLICKUP_API_KEY", "").strip()
    return workspace_id, api_key


async def execute_clickup_tool(name: str, tool_input: dict) -> str:
    """Dispatch a ClickUp tool call and return a formatted result string."""

    workspace_id, clickup_key = _clickup_creds()
    if not workspace_id or not clickup_key:
        return "ClickUp credentials not configured."

    if name == "get_clickup_tasks":
        include_closed = tool_input.get("include_closed", False)
        overdue_only   = tool_input.get("overdue_only", False)

        params: dict = {
            "include_closed": str(include_closed).lower(),
            "order_by": "due_date",
            "reverse": "false",
            "subtasks": "true",
            "limit": "50",
        }
        if overdue_only:
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
        for t in tasks[:20]:
            task_name  = t.get("name", "Untitled")
            status     = t.get("status", {}).get("status", "unknown")
            due        = t.get("due_date")
            due_str    = ""
            if due:
                due_str = f" — due {datetime.datetime.fromtimestamp(int(due)/1000).strftime('%b %d')}"
            assignees     = ", ".join(a.get("username", "") for a in t.get("assignees", []))
            assignee_str  = f" [{assignees}]" if assignees else ""
            lines.append(f"• {task_name} ({status}){assignee_str}{due_str}")
        return f"ClickUp tasks ({len(tasks)} total):\n" + "\n".join(lines)

    elif name == "list_clickup_workspace":
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
        lines  = []
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
        async with httpx.AsyncClient() as http:
            r = await http.get(
                f"https://api.clickup.com/api/v2/team/{workspace_id}",
                headers={"Authorization": clickup_key},
                timeout=15,
            )
        if r.status_code != 200:
            return f"ClickUp API error {r.status_code}: {r.text[:200]}"
        members = r.json().get("team", {}).get("members", [])
        lines   = []
        for m in members:
            u = m.get("user", {})
            lines.append(f"• {u.get('username', '')} — {u.get('email', '')} (id: {u.get('id', '')})")
        return "\n".join(lines) if lines else "No members found."

    elif name == "create_clickup_task":
        list_id      = tool_input["list_id"]
        task_name    = tool_input["name"]
        due_date_ms  = tool_input.get("due_date_ms")
        assignee_ids = tool_input.get("assignee_ids", [])
        description  = tool_input.get("description", "")

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

    return f"Unknown ClickUp tool: {name}"
