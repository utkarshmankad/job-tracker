"""MCP server definitions and tool allowlists for the Job Tracker agent."""

from dataclasses import dataclass, field


@dataclass
class MCPServer:
    name: str
    url: str
    allowed_tools: list[str] = field(default_factory=list)


GMAIL_SERVER = MCPServer(
    name="gmail",
    url="https://gmailmcp.googleapis.com/mcp/v1",
    allowed_tools=[
        "gmail_send_message",
        "gmail_create_draft",
        "gmail_list_messages",
        "gmail_get_message",
    ],
)

GDRIVE_SERVER = MCPServer(
    name="gdrive",
    url="https://drivemcp.googleapis.com/mcp/v1",
    allowed_tools=[
        "gdrive_list_files",
        "gdrive_get_file",
        "gdrive_search_files",
    ],
)

NOTION_SERVER = MCPServer(
    name="notion",
    url="https://mcp.notion.com/mcp",
    allowed_tools=[
        "notion_search",
        "notion_create_page",
        "notion_update_page",
        "notion_get_page",
    ],
)

MCP_SERVERS: list[MCPServer] = [GMAIL_SERVER, GDRIVE_SERVER, NOTION_SERVER]
