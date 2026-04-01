from __future__ import annotations

import logging
from typing import Any

import httpx

from ..models.pipeline import PipelineStage

logger = logging.getLogger(__name__)

BASE_URL = "https://api.clickup.com/api/v2"

# Statuses applied to every pipeline-stage List
STAGE_STATUSES = [
    {"status": "To Do", "color": "#d3d3d3", "type": "open"},
    {"status": "In Progress", "color": "#4a90e2", "type": "custom"},
    {"status": "Awaiting Approval", "color": "#f5a623", "type": "custom"},
    {"status": "Approved", "color": "#7ed321", "type": "custom"},
    {"status": "Revision Requested", "color": "#e74c3c", "type": "custom"},
    {"status": "Complete", "color": "#27ae60", "type": "closed"},
]


class ClickUpAPIError(Exception):
    """Raised when a ClickUp API call fails."""
    pass


class ClickUpClient:
    """
    Async wrapper around the ClickUp REST API v2.

    ClickUp has no official Python SDK, so all calls use httpx directly.
    All methods raise ClickUpAPIError on non-2xx responses.
    """

    def __init__(self, api_key: str, workspace_id: str) -> None:
        self._headers = {
            "Authorization": api_key,
            "Content-Type": "application/json",
        }
        self.workspace_id = workspace_id

    async def _get(self, path: str, params: dict | None = None) -> Any:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                f"{BASE_URL}{path}",
                headers=self._headers,
                params=params or {},
            )
        if not response.is_success:
            raise ClickUpAPIError(f"GET {path} failed ({response.status_code}): {response.text}")
        return response.json()

    async def _post(self, path: str, json: dict) -> Any:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"{BASE_URL}{path}",
                headers=self._headers,
                json=json,
            )
        if not response.is_success:
            raise ClickUpAPIError(f"POST {path} failed ({response.status_code}): {response.text}")
        return response.json()

    async def _put(self, path: str, json: dict) -> Any:
        async with httpx.AsyncClient() as client:
            response = await client.put(
                f"{BASE_URL}{path}",
                headers=self._headers,
                json=json,
            )
        if not response.is_success:
            raise ClickUpAPIError(f"PUT {path} failed ({response.status_code}): {response.text}")
        return response.json()

    # ── Workspace hierarchy ───────────────────────────────────────────────────

    async def get_spaces(self) -> list[dict]:
        """List all Spaces in the workspace."""
        data = await self._get(f"/team/{self.workspace_id}/space", params={"archived": "false"})
        return data.get("spaces", [])

    async def create_folder(self, space_id: str, name: str) -> str:
        """Create a Folder in a Space. Returns the folder ID."""
        data = await self._post(f"/space/{space_id}/folder", json={"name": name})
        return data["id"]

    async def get_folder(self, folder_id: str) -> dict:
        """Retrieve a Folder by ID."""
        return await self._get(f"/folder/{folder_id}")

    async def create_list(self, folder_id: str, name: str) -> str:
        """
        Create a List (pipeline stage) inside a Folder.
        Applies standard stage statuses automatically.
        Returns the list ID.
        """
        data = await self._post(
            f"/folder/{folder_id}/list",
            json={
                "name": name,
                "statuses": STAGE_STATUSES,
            },
        )
        return data["id"]

    # ── Tasks ─────────────────────────────────────────────────────────────────

    async def create_task(
        self,
        list_id: str,
        name: str,
        description: str = "",
        status: str = "To Do",
        assignees: list[int] | None = None,
        due_date_ms: int | None = None,
    ) -> str:
        """Create a task in a List. Returns the task ID."""
        payload: dict[str, Any] = {
            "name": name,
            "description": description,
            "status": status,
        }
        if assignees:
            payload["assignees"] = assignees
        if due_date_ms:
            payload["due_date"] = due_date_ms
        data = await self._post(f"/list/{list_id}/task", json=payload)
        return data["id"]

    async def update_task(
        self,
        task_id: str,
        status: str | None = None,
        description: str | None = None,
        name: str | None = None,
    ) -> None:
        """Update a task's status, description, or name."""
        payload: dict[str, Any] = {}
        if status is not None:
            payload["status"] = status
        if description is not None:
            payload["description"] = description
        if name is not None:
            payload["name"] = name
        await self._put(f"/task/{task_id}", json=payload)

    async def get_task(self, task_id: str) -> dict:
        """Retrieve a task by ID."""
        return await self._get(f"/task/{task_id}")

    async def get_tasks_in_list(self, list_id: str) -> list[dict]:
        """Get all tasks in a List."""
        data = await self._get(f"/list/{list_id}/task")
        return data.get("tasks", [])

    async def add_comment(self, task_id: str, comment_text: str) -> None:
        """Add a comment to a task."""
        await self._post(f"/task/{task_id}/comment", json={"comment_text": comment_text})

    # ── Client setup helper ───────────────────────────────────────────────────

    async def setup_client_folder(
        self,
        space_id: str,
        client_name: str,
    ) -> dict[str, str]:
        """
        Provision a new client in ClickUp:
        1. Create a Folder named after the client
        2. Create one List per pipeline stage (with standard statuses)

        Returns a dict mapping PipelineStage value → list_id.
        Also returns the folder_id under key "folder_id".
        """
        folder_id = await self.create_folder(space_id, client_name)
        logger.info(f"Created ClickUp folder '{client_name}' → {folder_id}")

        stage_to_list: dict[str, str] = {"folder_id": folder_id}

        for stage in PipelineStage:
            list_id = await self.create_list(folder_id, stage.value)
            stage_to_list[stage.value] = list_id
            logger.info(f"  Created list '{stage.value}' → {list_id}")

        return stage_to_list

    async def get_members(self) -> list[dict]:
        """Get all members in the workspace."""
        data = await self._get(f"/team/{self.workspace_id}/member")
        return data.get("members", [])
