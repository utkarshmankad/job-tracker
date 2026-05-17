"""Custom exceptions for the Job Tracker AI agent."""


class AgentError(Exception):
    """Base exception for all agent-level failures."""


class AuthError(AgentError):
    """Raised when an MCP server returns a 401 or 403 response."""

    def __init__(self, server_name: str, status_code: int) -> None:
        self.server_name = server_name
        self.status_code = status_code
        super().__init__(
            f"Auth error {status_code} from MCP server '{server_name}'"
        )


class LoopCapError(AgentError):
    """Raised when the agentic loop reaches max_iterations without end_turn."""

    def __init__(self, max_iterations: int) -> None:
        self.max_iterations = max_iterations
        super().__init__(
            f"Agent loop reached the {max_iterations}-iteration cap without "
            "a final response"
        )
