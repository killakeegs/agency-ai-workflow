"""
Anthropic tool schemas — defined once, imported by all agents.

Each constant is a dict matching the Anthropic API tool schema format.
Sub-agents import from here rather than re-declaring schemas.
"""

# ── Notion tools ──────────────────────────────────────────────────────────────

NOTION_GET_PAGE_TOOL: dict = {
    "name": "notion_get_page",
    "description": "Retrieve a Notion page by its ID. Returns the page properties and metadata.",
    "input_schema": {
        "type": "object",
        "properties": {
            "page_id": {
                "type": "string",
                "description": "The Notion page ID (UUID format).",
            }
        },
        "required": ["page_id"],
    },
}

NOTION_GET_BLOCKS_TOOL: dict = {
    "name": "notion_get_blocks",
    "description": "Get all content blocks (body text) from a Notion page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "page_id": {
                "type": "string",
                "description": "The Notion page ID.",
            }
        },
        "required": ["page_id"],
    },
}

NOTION_QUERY_DATABASE_TOOL: dict = {
    "name": "notion_query_database",
    "description": "Query entries in a Notion database, with optional filtering.",
    "input_schema": {
        "type": "object",
        "properties": {
            "database_id": {
                "type": "string",
                "description": "The Notion database ID.",
            },
            "filter": {
                "type": "object",
                "description": "Optional Notion filter object.",
            },
        },
        "required": ["database_id"],
    },
}

NOTION_CREATE_ENTRY_TOOL: dict = {
    "name": "notion_create_entry",
    "description": "Create a new entry in a Notion database.",
    "input_schema": {
        "type": "object",
        "properties": {
            "database_id": {
                "type": "string",
                "description": "The Notion database ID to add the entry to.",
            },
            "properties": {
                "type": "object",
                "description": "The property values for the new entry (Notion API format).",
            },
        },
        "required": ["database_id", "properties"],
    },
}

NOTION_UPDATE_ENTRY_TOOL: dict = {
    "name": "notion_update_entry",
    "description": "Update properties on an existing Notion database entry or page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "page_id": {
                "type": "string",
                "description": "The page ID of the entry to update.",
            },
            "properties": {
                "type": "object",
                "description": "The property values to update (Notion API format).",
            },
        },
        "required": ["page_id", "properties"],
    },
}

NOTION_APPEND_BLOCKS_TOOL: dict = {
    "name": "notion_append_blocks",
    "description": "Append content blocks (paragraphs, headings, bullet lists) to a Notion page.",
    "input_schema": {
        "type": "object",
        "properties": {
            "page_id": {
                "type": "string",
                "description": "The Notion page ID to append blocks to.",
            },
            "blocks": {
                "type": "array",
                "description": "Array of Notion block objects to append.",
                "items": {"type": "object"},
            },
        },
        "required": ["page_id", "blocks"],
    },
}

# ── ClickUp tools ─────────────────────────────────────────────────────────────

CLICKUP_CREATE_TASK_TOOL: dict = {
    "name": "clickup_create_task",
    "description": "Create a task in a ClickUp list.",
    "input_schema": {
        "type": "object",
        "properties": {
            "list_id": {
                "type": "string",
                "description": "The ClickUp list ID to create the task in.",
            },
            "name": {
                "type": "string",
                "description": "The task name.",
            },
            "description": {
                "type": "string",
                "description": "The task description (markdown supported).",
            },
            "status": {
                "type": "string",
                "description": "Initial task status. Defaults to 'To Do'.",
            },
        },
        "required": ["list_id", "name"],
    },
}

CLICKUP_UPDATE_TASK_TOOL: dict = {
    "name": "clickup_update_task",
    "description": "Update a ClickUp task's status, name, or description.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The ClickUp task ID.",
            },
            "status": {
                "type": "string",
                "description": "New status value.",
            },
            "description": {
                "type": "string",
                "description": "New description text.",
            },
        },
        "required": ["task_id"],
    },
}

CLICKUP_ADD_COMMENT_TOOL: dict = {
    "name": "clickup_add_comment",
    "description": "Add a comment to a ClickUp task.",
    "input_schema": {
        "type": "object",
        "properties": {
            "task_id": {
                "type": "string",
                "description": "The ClickUp task ID.",
            },
            "comment_text": {
                "type": "string",
                "description": "The comment text (markdown supported).",
            },
        },
        "required": ["task_id", "comment_text"],
    },
}

# ── Tool sets by agent ────────────────────────────────────────────────────────
# Pre-defined lists of tools for each agent type

TRANSCRIPT_PARSER_TOOLS = [
    NOTION_GET_PAGE_TOOL,
    NOTION_GET_BLOCKS_TOOL,
    NOTION_CREATE_ENTRY_TOOL,
    NOTION_APPEND_BLOCKS_TOOL,
    CLICKUP_CREATE_TASK_TOOL,
]

MOOD_BOARD_TOOLS = [
    NOTION_GET_PAGE_TOOL,
    NOTION_QUERY_DATABASE_TOOL,
    NOTION_CREATE_ENTRY_TOOL,
    NOTION_APPEND_BLOCKS_TOOL,
]

SITEMAP_TOOLS = [
    NOTION_GET_PAGE_TOOL,
    NOTION_QUERY_DATABASE_TOOL,
    NOTION_CREATE_ENTRY_TOOL,
    NOTION_APPEND_BLOCKS_TOOL,
]

CONTENT_TOOLS = [
    NOTION_GET_PAGE_TOOL,
    NOTION_QUERY_DATABASE_TOOL,
    NOTION_CREATE_ENTRY_TOOL,
    NOTION_UPDATE_ENTRY_TOOL,
    NOTION_APPEND_BLOCKS_TOOL,
]

WIREFRAME_TOOLS = [
    NOTION_GET_PAGE_TOOL,
    NOTION_QUERY_DATABASE_TOOL,
    NOTION_CREATE_ENTRY_TOOL,
    NOTION_APPEND_BLOCKS_TOOL,
]

HIFI_TOOLS = [
    NOTION_GET_PAGE_TOOL,
    NOTION_QUERY_DATABASE_TOOL,
    NOTION_CREATE_ENTRY_TOOL,
    NOTION_APPEND_BLOCKS_TOOL,
]

APPROVAL_HANDLER_TOOLS = [
    NOTION_GET_PAGE_TOOL,
    NOTION_CREATE_ENTRY_TOOL,
    NOTION_UPDATE_ENTRY_TOOL,
    CLICKUP_UPDATE_TASK_TOOL,
    CLICKUP_ADD_COMMENT_TOOL,
]
