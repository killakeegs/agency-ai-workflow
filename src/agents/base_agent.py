from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any

from anthropic import AsyncAnthropic
from anthropic.types import Message

from ..config import settings
from ..integrations.clickup import ClickUpClient
from ..integrations.notion import NotionClient

logger = logging.getLogger(__name__)


class AgentError(Exception):
    """Raised when an agent encounters an unrecoverable error."""
    pass


class BaseAgent(ABC):
    """
    Abstract base class for all pipeline agents.

    Provides:
    - Shared AsyncAnthropic client (reads ANTHROPIC_API_KEY from environment)
    - Shared Notion and ClickUp clients
    - _run_tool_loop(): the Anthropic tool-use agentic loop
    - Structured logging via Python's standard logging module

    Sub-classes must implement:
    - name (class attribute): human-readable agent name used in logs
    - tools (class attribute): list of Anthropic tool schema dicts
    - run(client_id, **kwargs) -> dict: main entry point, returns structured output
    - _execute_tool(tool_name, tool_input) -> str: dispatches tool calls to integrations
    """

    name: str = "base_agent"
    tools: list[dict] = []

    def __init__(
        self,
        notion: NotionClient,
        model: str,
        max_tokens: int = 4096,
        clickup: ClickUpClient | None = None,
    ) -> None:
        self.notion = notion
        self.clickup = clickup
        self.anthropic = AsyncAnthropic(api_key=settings.anthropic_api_key)
        self.model = model
        self.max_tokens = max_tokens
        self.log = logging.getLogger(f"agent.{self.name}")

    async def _run_tool_loop(
        self,
        system_prompt: str,
        user_message: str,
        tools: list[dict] | None = None,
    ) -> str:
        """
        Execute the Anthropic messages API in a tool-use loop.

        Calls the API repeatedly until Claude returns stop_reason="end_turn".
        Between each call, dispatches any tool_use blocks to _execute_tool()
        and appends the results as tool_result blocks.

        Returns the final text output from Claude.
        """
        active_tools = tools if tools is not None else self.tools
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": user_message}
        ]

        self.log.debug(f"Starting tool loop | model={self.model} | tools={[t['name'] for t in active_tools]}")

        while True:
            response: Message = await self.anthropic.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                system=system_prompt,
                tools=active_tools,
                messages=messages,
            )

            self.log.debug(f"Response stop_reason={response.stop_reason} | input_tokens={response.usage.input_tokens} | output_tokens={response.usage.output_tokens}")

            if response.stop_reason == "end_turn":
                return self._extract_text(response)

            if response.stop_reason == "tool_use":
                # Append Claude's response (including tool_use blocks)
                messages.append({"role": "assistant", "content": response.content})

                # Execute each tool call and collect results
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if block.type == "tool_use":
                        self.log.info(f"Tool call: {block.name} | input={block.input}")
                        try:
                            result = await self._execute_tool(block.name, block.input)
                        except Exception as e:
                            result = f"ERROR: {e}"
                            self.log.error(f"Tool {block.name} failed: {e}")

                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })

                messages.append({"role": "user", "content": tool_results})

            else:
                raise AgentError(
                    f"Unexpected stop_reason from Claude: {response.stop_reason}"
                )

    @staticmethod
    def _extract_text(response: Message) -> str:
        """Extract the first text block from a Claude response."""
        for block in response.content:
            if hasattr(block, "text"):
                return block.text
        return ""

    @abstractmethod
    async def _execute_tool(self, tool_name: str, tool_input: dict) -> str:
        """
        Dispatch a tool call to the appropriate integration.

        Receives the tool name and input dict from Claude's tool_use block.
        Must return a string result that will be sent back to Claude as tool_result.

        Raise AgentError for unrecoverable failures.
        """

    @abstractmethod
    async def run(self, client_id: str, **kwargs: Any) -> dict:
        """
        Main entry point for the agent.

        Reads client context from Notion/ClickUp, calls _run_tool_loop,
        writes structured output back to Notion, and returns a summary dict.

        The returned dict should always include:
        - "status": "success" | "error"
        - "stage": the PipelineStage this agent handled
        - "notion_page_id": ID of the output written to Notion (if applicable)
        """
