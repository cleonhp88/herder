"""Stub ACP agent for integration testing.

Usage:
    python tests/acp_stub_agent.py <mode>

Modes:
    echo       Stream two agent_message_chunk updates ("Hello " + "world"), then end_turn.
    permission Send a request_permission call; reply TOOL_ALLOWED or TOOL_DENIED.
    slow       Sleep 30s before responding (for timeout tests).
    refuse     End turn with stop_reason "refusal".
"""
from __future__ import annotations

import asyncio
import sys
from typing import Any


class _StubAgent:
    """Agent-side stub that implements the ACP Agent protocol.

    Args:
        mode: Behaviour mode string ("echo", "permission", "slow", "refuse").
    """

    def __init__(self, mode: str) -> None:
        self._mode = mode
        self._conn: Any = None

    def on_connect(self, conn: Any) -> None:
        """Store the AgentSideConnection for use during prompt handling."""
        self._conn = conn

    async def initialize(self, protocol_version: int, **kwargs: Any) -> Any:
        """Respond to initialize handshake."""
        from acp.schema import AgentCapabilities, InitializeResponse, Implementation
        return InitializeResponse(
            protocol_version=protocol_version,
            agent_capabilities=AgentCapabilities(),
            agent_info=Implementation(name="stub-agent", version="0.0.1"),
        )

    async def new_session(self, cwd: str, **kwargs: Any) -> Any:
        """Create a new session and return its ID."""
        from acp.schema import NewSessionResponse
        return NewSessionResponse(session_id="stub-session-001")

    async def prompt(self, prompt: list[Any], session_id: str, **kwargs: Any) -> Any:
        """Handle a prompt according to the configured mode."""
        from acp.schema import PromptResponse
        import acp

        if self._mode == "echo":
            # Stream two chunks then end_turn
            await self._conn.session_update(
                session_id=session_id,
                update=acp.update_agent_message_text("Hello "),
            )
            await self._conn.session_update(
                session_id=session_id,
                update=acp.update_agent_message_text("world"),
            )
            return PromptResponse(stop_reason="end_turn")

        if self._mode == "permission":
            # Request permission with allow/reject options
            from acp.schema import PermissionOption, ToolCallUpdate
            options = [
                PermissionOption(
                    kind="allow_once",
                    name="Allow once",
                    option_id="allow-once-id",
                ),
                PermissionOption(
                    kind="reject_once",
                    name="Reject",
                    option_id="reject-once-id",
                ),
            ]
            tool_call = ToolCallUpdate(tool_call_id="tool-001")
            resp = await self._conn.request_permission(
                options=options,
                session_id=session_id,
                tool_call=tool_call,
            )
            # Determine what the client decided
            outcome_type = type(resp.outcome).__name__
            if outcome_type == "AllowedOutcome":
                reply = "TOOL_ALLOWED"
            else:
                reply = "TOOL_DENIED"
            await self._conn.session_update(
                session_id=session_id,
                update=acp.update_agent_message_text(reply),
            )
            return PromptResponse(stop_reason="end_turn")

        if self._mode == "slow":
            # Sleep long enough to trigger timeout test (test uses timeout=3s)
            await asyncio.sleep(30)
            return PromptResponse(stop_reason="end_turn")

        if self._mode == "refuse":
            return PromptResponse(stop_reason="refusal")

        # Unknown mode: end_turn with no output
        return PromptResponse(stop_reason="end_turn")

    # ------------------------------------------------------------------
    # Required by protocol but not exercised by stub
    # ------------------------------------------------------------------

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        """Handle cancellation notification."""

    async def load_session(self, cwd: str, session_id: str, **kwargs: Any) -> None:
        """Return None (session not found)."""
        return None

    async def list_sessions(self, **kwargs: Any) -> Any:
        """Return empty session list."""
        from acp.schema import ListSessionsResponse
        return ListSessionsResponse(sessions=[], next_cursor=None)

    async def fork_session(self, cwd: str, session_id: str, **kwargs: Any) -> Any:
        """Not implemented in stub."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found("session/fork")

    async def resume_session(self, cwd: str, session_id: str, **kwargs: Any) -> Any:
        """Not implemented in stub."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found("session/resume")

    async def close_session(self, session_id: str, **kwargs: Any) -> None:
        """No-op close."""
        return None

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any) -> None:
        """Not implemented in stub."""
        return None

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> None:
        """Not implemented in stub."""
        return None

    async def set_config_option(self, config_id: str, session_id: str, value: Any, **kwargs: Any) -> None:
        """Not implemented in stub."""
        return None

    async def authenticate(self, method_id: str, **kwargs: Any) -> None:
        """Not implemented in stub."""
        return None

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Not implemented in stub."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        """Silently ignore extension notifications."""


async def _main() -> None:
    """Entry point: parse mode argument and run the agent over stdio."""
    import acp

    mode = sys.argv[1] if len(sys.argv) > 1 else "echo"
    agent = _StubAgent(mode)
    await acp.run_agent(agent)


if __name__ == "__main__":
    asyncio.run(_main())
