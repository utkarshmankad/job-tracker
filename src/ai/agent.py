"""Async agentic loop for the Job Tracker AI agent.

Connects to the Anthropic Messages API with three MCP servers (Gmail, Google
Drive, Notion) and drives the tool-call loop until Claude returns a final
text response or the iteration cap is reached.
"""

import logging
import os
from typing import Any

import anthropic
from dotenv import load_dotenv

from .exceptions import AgentError, AuthError, LoopCapError
from .mcp_config import MCP_SERVERS, MCPServer
from .prompts import SYSTEM_PROMPT, build_user_message

load_dotenv()

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_BETA_HEADER = "mcp-client-2025-11-20"


def _build_mcp_server_param(server: MCPServer) -> dict[str, Any]:
    return {
        "type": "url",
        "url": server.url,
        "name": server.name,
        "allowed_tools": server.allowed_tools,
    }


def _extract_text(response: anthropic.types.Message) -> str:
    """Return concatenated text from all TextBlock content items."""
    parts: list[str] = []
    for block in response.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    return "\n".join(parts)


def _check_auth_error(exc: Exception) -> None:
    """Re-raise as AuthError when the underlying cause is a 401/403."""
    status: int | None = None
    if isinstance(exc, anthropic.APIStatusError):
        status = exc.status_code
    if status in (401, 403):
        raise AuthError(server_name="mcp", status_code=status) from exc


async def run_agent(
    user_instruction: str,
    conversation_history: list[dict[str, Any]] | None = None,
    max_iterations: int = 10,
) -> str:
    """Run the Job Tracker agent with the given user instruction.

    Args:
        user_instruction: Natural language task from the user.
        conversation_history: Optional prior messages for multi-turn support.
        max_iterations: Safety cap on the agentic loop.

    Returns:
        Final text response from Claude.

    Raises:
        AuthError: If an MCP server returns 401 or 403.
        LoopCapError: If max_iterations is reached without end_turn.
        AgentError: For any other agent-level failures.
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise AgentError("ANTHROPIC_API_KEY is not set in the environment")

    client = anthropic.AsyncAnthropic(api_key=api_key)
    mcp_servers = [_build_mcp_server_param(s) for s in MCP_SERVERS]

    messages: list[dict[str, Any]] = list(conversation_history or [])
    messages.append(build_user_message(user_instruction))

    for iteration in range(max_iterations):
        logger.debug("Agent loop iteration %d/%d", iteration + 1, max_iterations)

        try:
            response = await client.beta.messages.create(
                model=_MODEL,
                max_tokens=4096,
                system=SYSTEM_PROMPT,
                messages=messages,
                mcp_servers=mcp_servers,  # type: ignore[arg-type]
                betas=[_BETA_HEADER],
            )
        except anthropic.APIStatusError as exc:
            _check_auth_error(exc)
            raise AgentError(f"Anthropic API error: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise AgentError(f"Network error reaching Anthropic API: {exc}") from exc

        # Log every tool call at DEBUG level.
        for block in response.content:
            if block.type == "tool_use":
                logger.debug(
                    "Tool call: name=%s input=%s", block.name, block.input
                )

        if response.stop_reason == "end_turn":
            return _extract_text(response)

        # Collect tool_use blocks and build tool_result replies.
        tool_use_blocks = [b for b in response.content if b.type == "tool_use"]
        if not tool_use_blocks:
            # stop_reason is not end_turn but there are no tool calls — return
            # whatever text is present rather than looping forever.
            return _extract_text(response)

        # Append the assistant turn to the conversation.
        messages.append({"role": "assistant", "content": response.content})

        # Build tool_result blocks for every pending tool call.
        tool_results: list[dict[str, Any]] = []
        for block in tool_use_blocks:
            result_content = block.content if hasattr(block, "content") else []
            if not result_content:
                result_content = [{"type": "text", "text": ""}]
            tool_results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result_content,
                }
            )

        messages.append({"role": "user", "content": tool_results})

    raise LoopCapError(max_iterations)
