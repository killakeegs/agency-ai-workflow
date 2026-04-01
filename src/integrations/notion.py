from __future__ import annotations

import logging
from typing import Any

from notion_client import AsyncClient
from notion_client.errors import APIResponseError

logger = logging.getLogger(__name__)


class NotionAPIError(Exception):
    """Raised when a Notion API call fails."""
    pass


class NotionClient:
    """
    Async wrapper around the Notion API.

    All methods raise NotionAPIError on failure rather than letting
    notion_client exceptions propagate, so callers get a consistent
    error type regardless of the underlying failure mode.
    """

    def __init__(self, api_key: str) -> None:
        # Pin to 2022-06-28 — the 2025-09-03 version shipped in notion-client v3
        # removed the /databases/{id}/query endpoint from the SDK shortcuts.
        self._client = AsyncClient(auth=api_key, notion_version="2022-06-28")

    # ── Page operations ───────────────────────────────────────────────────────

    async def get_page(self, page_id: str) -> dict[str, Any]:
        """Retrieve a Notion page by ID."""
        try:
            return await self._client.pages.retrieve(page_id=page_id)
        except APIResponseError as e:
            raise NotionAPIError(f"Failed to get page {page_id}: {e}") from e

    async def create_page(
        self,
        parent_page_id: str,
        title: str,
        properties: dict[str, Any] | None = None,
    ) -> str:
        """
        Create a new page under a parent page.
        Returns the new page's ID.
        """
        payload: dict[str, Any] = {
            "parent": {"page_id": parent_page_id},
            "properties": {
                "title": {
                    "title": [{"text": {"content": title}}]
                }
            },
        }
        if properties:
            payload["properties"].update(properties)
        try:
            result = await self._client.pages.create(**payload)
            return result["id"]
        except APIResponseError as e:
            raise NotionAPIError(f"Failed to create page '{title}': {e}") from e

    async def update_page(self, page_id: str, properties: dict[str, Any]) -> None:
        """Update properties on an existing page."""
        try:
            await self._client.pages.update(page_id=page_id, properties=properties)
        except APIResponseError as e:
            raise NotionAPIError(f"Failed to update page {page_id}: {e}") from e

    # ── Database operations ───────────────────────────────────────────────────

    async def create_database(
        self,
        parent_page_id: str,
        title: str,
        properties_schema: dict[str, Any],
    ) -> str:
        """
        Create a new database under a parent page.
        Returns the new database's ID.

        The properties_schema dict follows Notion API format:
        {"Field Name": {"rich_text": {}}, "Status": {"select": {"options": [...]}}}
        """
        # notion-client v3 dropped "properties" from databases.create pick() — use request()
        body = {
            "parent": {"type": "page_id", "page_id": parent_page_id},
            "title": [{"text": {"content": title}}],
            "properties": properties_schema,
        }
        try:
            result = await self._client.request(
                path="databases",
                method="POST",
                body=body,
            )
            return result["id"]
        except APIResponseError as e:
            raise NotionAPIError(f"Failed to create database '{title}': {e}") from e

    async def update_database(
        self,
        database_id: str,
        properties_schema: dict[str, Any],
    ) -> None:
        """
        Update an existing database's property schema.
        Used in the second pass of setup_notion.py to add relation properties.
        """
        # notion-client v3 dropped "properties" from databases.update pick() — use request()
        try:
            await self._client.request(
                path=f"databases/{database_id}",
                method="PATCH",
                body={"properties": properties_schema},
            )
        except APIResponseError as e:
            raise NotionAPIError(f"Failed to update database {database_id}: {e}") from e

    async def query_database(
        self,
        database_id: str,
        filter_payload: dict[str, Any] | None = None,
        sorts: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """
        Query all entries in a database. Handles pagination automatically.
        Returns a flat list of page objects.
        """
        results: list[dict[str, Any]] = []
        kwargs: dict[str, Any] = {"database_id": database_id}
        if filter_payload:
            kwargs["filter"] = filter_payload
        if sorts:
            kwargs["sorts"] = sorts

        # databases.query() was removed in notion-client v3.0.0 — use request() directly
        try:
            has_more = True
            next_cursor: str | None = None
            while has_more:
                body: dict[str, Any] = {}
                if filter_payload:
                    body["filter"] = filter_payload
                if sorts:
                    body["sorts"] = sorts
                if next_cursor:
                    body["start_cursor"] = next_cursor
                response = await self._client.request(
                    path=f"databases/{database_id}/query",
                    method="POST",
                    body=body,
                )
                results.extend(response.get("results", []))
                has_more = response.get("has_more", False)
                next_cursor = response.get("next_cursor")
            return results
        except APIResponseError as e:
            raise NotionAPIError(f"Failed to query database {database_id}: {e}") from e

    async def create_database_entry(
        self,
        database_id: str,
        properties: dict[str, Any],
    ) -> str:
        """
        Create a new entry (page) in a database.
        Returns the new entry's page ID.
        """
        payload = {
            "parent": {"database_id": database_id},
            "properties": properties,
        }
        try:
            result = await self._client.pages.create(**payload)
            return result["id"]
        except APIResponseError as e:
            raise NotionAPIError(f"Failed to create entry in database {database_id}: {e}") from e

    async def update_database_entry(
        self,
        page_id: str,
        properties: dict[str, Any],
    ) -> None:
        """Update properties on an existing database entry."""
        try:
            await self._client.pages.update(page_id=page_id, properties=properties)
        except APIResponseError as e:
            raise NotionAPIError(f"Failed to update entry {page_id}: {e}") from e

    # ── Block operations ──────────────────────────────────────────────────────

    async def append_blocks(
        self,
        page_id: str,
        blocks: list[dict[str, Any]],
    ) -> None:
        """
        Append content blocks to a page.
        Used to write rich content like transcripts, mood board rationale, etc.

        Block format follows Notion block objects:
        [{"object": "block", "type": "paragraph", "paragraph": {"rich_text": [...]}}]
        """
        try:
            await self._client.blocks.children.append(
                block_id=page_id,
                children=blocks,
            )
        except APIResponseError as e:
            raise NotionAPIError(f"Failed to append blocks to page {page_id}: {e}") from e

    async def get_block_children(self, page_id: str) -> list[dict[str, Any]]:
        """Retrieve all content blocks on a page."""
        try:
            response = await self._client.blocks.children.list(block_id=page_id)
            return response.get("results", [])
        except APIResponseError as e:
            raise NotionAPIError(f"Failed to get blocks for page {page_id}: {e}") from e

    # ── Convenience helpers ───────────────────────────────────────────────────

    @staticmethod
    def text_property(value: str) -> dict[str, Any]:
        """Build a Notion rich_text property value."""
        return {"rich_text": [{"text": {"content": value}}]}

    @staticmethod
    def title_property(value: str) -> dict[str, Any]:
        """Build a Notion title property value."""
        return {"title": [{"text": {"content": value}}]}

    @staticmethod
    def select_property(value: str) -> dict[str, Any]:
        """Build a Notion select property value."""
        return {"select": {"name": value}}

    @staticmethod
    def checkbox_property(value: bool) -> dict[str, Any]:
        """Build a Notion checkbox property value."""
        return {"checkbox": value}

    @staticmethod
    def url_property(value: str) -> dict[str, Any]:
        """Build a Notion URL property value."""
        return {"url": value}

    @staticmethod
    def paragraph_block(text: str) -> dict[str, Any]:
        """Build a simple paragraph block for append_blocks."""
        return {
            "object": "block",
            "type": "paragraph",
            "paragraph": {
                "rich_text": [{"type": "text", "text": {"content": text}}]
            },
        }

    @staticmethod
    def heading_block(text: str, level: int = 2) -> dict[str, Any]:
        """Build a heading block (level 1, 2, or 3)."""
        heading_type = f"heading_{level}"
        return {
            "object": "block",
            "type": heading_type,
            heading_type: {
                "rich_text": [{"type": "text", "text": {"content": text}}]
            },
        }

    @staticmethod
    def bulleted_list_block(text: str) -> dict[str, Any]:
        """Build a bulleted list item block."""
        return {
            "object": "block",
            "type": "bulleted_list_item",
            "bulleted_list_item": {
                "rich_text": [{"type": "text", "text": {"content": text}}]
            },
        }
