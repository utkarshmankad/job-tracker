"""Tests for src/ai/agent.py.

All Anthropic API calls are mocked via AsyncMock — no real network traffic.
"""

import pytest
import pytest_asyncio  # noqa: F401 — registers asyncio mode
from unittest.mock import AsyncMock, MagicMock, patch

from src.ai.agent import run_agent
from src.ai.exceptions import AgentError, AuthError, LoopCapError


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_text_block(text: str) -> MagicMock:
    block = MagicMock()
    block.type = "text"
    block.text = text
    return block


def _make_tool_use_block(tool_id: str, name: str, inp: dict) -> MagicMock:
    block = MagicMock()
    block.type = "tool_use"
    block.id = tool_id
    block.name = name
    block.input = inp
    block.content = []  # MCP returns results in block.content
    return block


def _make_response(
    stop_reason: str = "end_turn",
    content: list | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.stop_reason = stop_reason
    resp.content = content or [_make_text_block("Done.")]
    return resp


# ---------------------------------------------------------------------------
# Test 1 — single-turn "find emails from Atlassian"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_find_emails_atlassian(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent should return Claude's final text when stop_reason is end_turn."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    final_response = _make_response(
        stop_reason="end_turn",
        content=[_make_text_block("Found 3 emails from Atlassian in the last 30 days.")],
    )

    mock_create = AsyncMock(return_value=final_response)
    with patch("src.ai.agent.anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.beta.messages.create = mock_create
        result = await run_agent(
            "Find any emails from Atlassian in the last 30 days"
        )

    assert "Atlassian" in result
    mock_create.assert_awaited_once()


# ---------------------------------------------------------------------------
# Test 2 — multi-turn with a tool call: "Update Notion job tracker for Swiggy"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_notion_with_tool_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent should handle one tool call round-trip and return final text."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    tool_block = _make_tool_use_block(
        tool_id="tool_1",
        name="notion_update_page",
        inp={"page_id": "abc123", "status": "Interview Scheduled"},
    )
    # First response triggers a tool call.
    first_response = _make_response(
        stop_reason="tool_use",
        content=[tool_block],
    )
    # Second response is the final answer.
    second_response = _make_response(
        stop_reason="end_turn",
        content=[_make_text_block("Swiggy application updated to Interview Scheduled.")],
    )

    mock_create = AsyncMock(side_effect=[first_response, second_response])
    with patch("src.ai.agent.anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.beta.messages.create = mock_create
        result = await run_agent(
            "Update my Notion job tracker: mark Swiggy application as Interview Scheduled"
        )

    assert "Swiggy" in result
    assert mock_create.await_count == 2


# ---------------------------------------------------------------------------
# Test 3 — "Summarize job-related emails this week"
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_summarize_job_emails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent should return a non-empty summary string."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    summary_text = (
        "This week you received 5 job-related emails: 2 interview invites, "
        "1 rejection, and 2 application acknowledgements."
    )
    mock_create = AsyncMock(
        return_value=_make_response(
            stop_reason="end_turn",
            content=[_make_text_block(summary_text)],
        )
    )
    with patch("src.ai.agent.anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.beta.messages.create = mock_create
        result = await run_agent(
            "Summarize all job-related emails I received this week"
        )

    assert len(result) > 0
    assert "interview" in result.lower()


# ---------------------------------------------------------------------------
# Test 4 — LoopCapError raised at max_iterations
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_loop_cap_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """LoopCapError must be raised when every response contains a tool call."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    # Every API response contains a pending tool call → loop never ends.
    def _perpetual_tool_response() -> MagicMock:
        return _make_response(
            stop_reason="tool_use",
            content=[
                _make_tool_use_block("t_id", "gmail_list_messages", {"query": "job"})
            ],
        )

    mock_create = AsyncMock(side_effect=lambda **_: _perpetual_tool_response())
    with patch("src.ai.agent.anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.beta.messages.create = mock_create
        with pytest.raises(LoopCapError) as exc_info:
            await run_agent("Find all job emails", max_iterations=3)

    assert exc_info.value.max_iterations == 3
    assert mock_create.await_count == 3


# ---------------------------------------------------------------------------
# Test 5 — AuthError raised on 401 from Anthropic
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auth_error_on_401(monkeypatch: pytest.MonkeyPatch) -> None:
    """AuthError must be raised when the API returns a 401."""
    import anthropic as _anthropic

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-bad-key")

    http_response = MagicMock()
    http_response.status_code = 401
    api_err = _anthropic.AuthenticationError(
        message="Unauthorized",
        response=http_response,
        body={"error": {"message": "Unauthorized"}},
    )

    mock_create = AsyncMock(side_effect=api_err)
    with patch("src.ai.agent.anthropic.AsyncAnthropic") as MockClient:
        MockClient.return_value.beta.messages.create = mock_create
        with pytest.raises(AuthError) as exc_info:
            await run_agent("Find emails")

    assert exc_info.value.status_code == 401
