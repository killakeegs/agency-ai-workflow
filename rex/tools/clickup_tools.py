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
    "update_clickup_task",
    "search_clickup_tasks",
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

    elif name == "update_clickup_task":
        task_id = tool_input["task_id"]
        body: dict = {}
        if "status" in tool_input:
            body["status"] = tool_input["status"]
        if "name" in tool_input:
            body["name"] = tool_input["name"]
        if "description" in tool_input:
            body["description"] = tool_input["description"]
        if "due_date_ms" in tool_input:
            body["due_date"] = tool_input["due_date_ms"]
        if "start_date_ms" in tool_input:
            body["start_date"] = tool_input["start_date_ms"]
        if "priority" in tool_input:
            body["priority"] = tool_input["priority"]
        if "assignees_add" in tool_input or "assignees_remove" in tool_input:
            body["assignees"] = {
                "add": tool_input.get("assignees_add", []),
                "rem": tool_input.get("assignees_remove", []),
            }

        if not body:
            return "Nothing to update — provide at least one field (status, name, due_date_ms, priority, etc.)"

        async with httpx.AsyncClient() as http:
            r = await http.put(
                f"https://api.clickup.com/api/v2/task/{task_id}",
                headers={"Authorization": clickup_key, "Content-Type": "application/json"},
                json=body,
                timeout=15,
            )
        if r.status_code != 200:
            return f"Failed to update task {task_id}: {r.status_code} — {r.text[:200]}"
        task = r.json()
        return f"Task updated: *{task.get('name')}* (id: {task_id}) — status: {task.get('status', {}).get('status', 'n/a')}"

    elif name == "search_clickup_tasks":
        query = tool_input.get("query", "").strip()
        if not query:
            return "Provide a search query (task name keyword)."

        params: dict = {
            "include_closed": str(tool_input.get("include_closed", False)).lower(),
            "order_by": "due_date",
            "reverse": "false",
            "subtasks": "true",
            "limit": "100",
        }

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
        q_lower = query.lower()
        matches = [t for t in tasks if q_lower in (t.get("name", "") + " " + (t.get("description") or "")).lower()]
        if not matches:
            return f"No tasks found matching '{query}'."

        lines = [f"Found {len(matches)} task(s) matching '{query}':"]
        for t in matches[:15]:
            status = t.get("status", {}).get("status", "n/a")
            assignees = ", ".join(a.get("username", "") for a in t.get("assignees", [])) or "unassigned"
            due_ms = t.get("due_date")
            due_str = ""
            if due_ms:
                due_str = f" | due {datetime.datetime.fromtimestamp(int(due_ms)/1000).strftime('%Y-%m-%d')}"
            lines.append(f"  • [{status}] {t.get('name', '')} (id: {t.get('id')}) — {assignees}{due_str}")
        return "\n".join(lines)

    return f"Unknown ClickUp tool: {name}"
