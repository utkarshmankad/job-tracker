# Job Tracker AI Agent

The agent module lives at `src/ai/` and drives a multi-turn Anthropic API loop
that can read Gmail, Google Drive, and Notion on the user's behalf.

---

## How to add a new MCP server

1. Open `src/ai/mcp_config.py`.
2. Create a new `MCPServer` instance — supply a `name`, the server `url`, and
   an explicit `allowed_tools` list (never use `"*"`):

   ```python
   CALENDAR_SERVER = MCPServer(
       name="calendar",
       url="https://calendarmcp.googleapis.com/mcp/v1",
       allowed_tools=["calendar_list_events", "calendar_create_event"],
   )
   ```

3. Append it to `MCP_SERVERS`:

   ```python
   MCP_SERVERS: list[MCPServer] = [GMAIL_SERVER, GDRIVE_SERVER, NOTION_SERVER, CALENDAR_SERVER]
   ```

That's it. `agent.py` reads `MCP_SERVERS` at call time, so no other file needs
to change.

---

## How to extend the system prompt

Open `src/ai/prompts.py` and edit `SYSTEM_PROMPT`. The string is sent as the
`system` field on every API call. Good additions:

- New behavioral guardrails ("never share salary details with third parties")
- Tool-usage guidance ("prefer searching Drive before drafting a new document")
- Output format requirements ("always return action items as a numbered list")

If you need per-instruction prompts (e.g., different tone for cover letters
vs. status updates), extend `build_user_message` or add a second builder:

```python
def build_cover_letter_message(instruction: str, job_title: str) -> dict[str, str]:
    prefix = f"You are writing a cover letter for the role: {job_title}.\n\n"
    return {"role": "user", "content": prefix + instruction}
```

Pass the result directly as `user_instruction` — or extend `run_agent`'s
signature to accept an optional `system_override` parameter if you need to
swap the system prompt entirely.

---

## How to enable DEBUG logging to trace tool calls

The agent logs every tool call at `DEBUG` level using Python's `logging` module.
To see these logs, configure the root logger (or the `src.ai.agent` logger)
before calling `run_agent`:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
```

Or, to target only the agent without flooding other libraries:

```python
logging.getLogger("src.ai.agent").setLevel(logging.DEBUG)
logging.getLogger("src.ai.agent").addHandler(logging.StreamHandler())
```

Each log line looks like:

```
DEBUG src.ai.agent - Tool call: name=gmail_list_messages input={'query': 'from:atlassian', 'max_results': 10}
```

The iteration counter is also logged at DEBUG, which makes it easy to see how
many round-trips a task required.
