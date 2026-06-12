"""ACP (Agent Client Protocol) provider adapter — opt-in, requires extra dep.

Wire: newline-delimited JSON-RPC 2.0 over stdio.  The SDK's spawn_agent_process
spawns a subprocess and binds its stdin/stdout as asyncio streams.

Threading note: queue_claim runs jobs on a ThreadPoolExecutor.  asyncio.run()
creates a new event loop per call, which is safe when each call runs in its own
OS thread — Python guarantees no loop is shared across threads here.

env interplay: spawn_agent_process passes the caller-supplied ``env`` mapping to
spawn_stdio_transport, which calls ``default_environment()`` (HOME/PATH/SHELL/TERM/
USER/LOGNAME on POSIX) then *merges the caller env on top*.  This means herder's
minimised env (built by build_env()) replaces the SDK defaults for any key that
appears in both.  Keys herder omits but the SDK baseline includes (e.g. TERM) will
still be present.  That is intentional — the provider process needs a functional
PATH.  Herder's env_profile controls which *secrets* pass through; low-level PATH
etc. are handled by the SDK baseline, preventing broken subprocess environments.
"""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from herder.models import Result

logger = logging.getLogger(__name__)


def _now() -> datetime:
    """Return current UTC timestamp."""
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Headless client implementation
# ---------------------------------------------------------------------------

class _HeadlessClient:
    """Minimal ACP Client that accumulates streamed text and applies a permission policy.

    Args:
        allow_tools: If True, prefer the first ``allow_*`` option when permission
            is requested.  If False, always deny (suitable for read-only jobs).
    """

    def __init__(self, allow_tools: bool = False) -> None:
        self._allow_tools = allow_tools
        # Accumulated text chunks from agent_message_chunk session updates
        self._chunks: list[str] = []

    @property
    def accumulated_text(self) -> str:
        """Return all accumulated text chunks joined."""
        return "".join(self._chunks)

    # ------------------------------------------------------------------
    # Client protocol — required methods
    # ------------------------------------------------------------------

    async def request_permission(
        self,
        options: list[Any],
        session_id: str,
        tool_call: Any,
        **kwargs: Any,
    ) -> Any:
        """Apply the injected permission policy.

        If allow_tools is True, select the first option whose kind starts with
        ``allow``; if none found, deny.  If allow_tools is False, always deny.
        """
        # Import here so acp is only loaded when this module is used
        from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse

        if self._allow_tools:
            # Prefer allow_once over allow_always (least-privilege); fall back to
            # any option whose kind starts with "allow".
            allow_once = next((o for o in options if str(o.kind) == "allow_once"), None)
            allow_any = next((o for o in options if str(o.kind).startswith("allow")), None)
            chosen = allow_once or allow_any
            if chosen is not None:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(outcome="selected", option_id=chosen.option_id)
                )

        # Default: deny
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def session_update(
        self,
        session_id: str,
        update: Any,
        **kwargs: Any,
    ) -> None:
        """Accumulate text chunks from agent_message_chunk updates."""
        # Only collect text from agent message chunks; ignore thought/plan/tool updates
        session_update_kind = getattr(update, "session_update", None)
        if session_update_kind == "agent_message_chunk":
            content = getattr(update, "content", None)
            if content is not None:
                # ContentChunk.content is a single block (discriminated union)
                text = getattr(content, "text", None)
                if text is not None:
                    self._chunks.append(text)

    # ------------------------------------------------------------------
    # File-system methods — not supported in headless mode
    # ------------------------------------------------------------------

    async def write_text_file(self, content: str, path: str, session_id: str, **kwargs: Any) -> None:
        """Raise method_not_found — headless client does not support file writes."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(self, path: str, session_id: str, **kwargs: Any) -> Any:
        """Raise method_not_found — headless client does not support file reads."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found("fs/read_text_file")

    # ------------------------------------------------------------------
    # Terminal methods — not supported in headless mode
    # ------------------------------------------------------------------

    async def create_terminal(self, command: str, session_id: str, **kwargs: Any) -> Any:
        """Raise method_not_found — headless client does not support terminals."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        """Raise method_not_found — headless client does not support terminals."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> None:
        """Raise method_not_found — headless client does not support terminals."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        """Raise method_not_found — headless client does not support terminals."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> None:
        """Raise method_not_found — headless client does not support terminals."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found("terminal/kill")

    # ------------------------------------------------------------------
    # Extension stubs
    # ------------------------------------------------------------------

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Raise method_not_found for all extension methods."""
        from acp.exceptions import RequestError
        raise RequestError.method_not_found(method)

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        """Silently ignore extension notifications."""

    def on_connect(self, conn: Any) -> None:
        """Called when the connection is established; no action needed."""


# ---------------------------------------------------------------------------
# Async core
# ---------------------------------------------------------------------------

async def _run_async(
    provider_executable: str,
    provider_args: list[str],
    prompt: str,
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: float,
    allow_tools: bool,
    cancel_check: Callable[[], bool] | None,
    heartbeat: Callable[[], None] | None,
    heartbeat_interval: float,
    stderr_path: Path | None,
) -> Result:
    """Async implementation of ACP provider execution.

    Args:
        provider_executable: Path to the agent executable.
        provider_args: Additional arguments for the agent.
        prompt: Input prompt text.
        cwd: Working directory.
        env: Environment variables.
        timeout: Execution timeout in seconds.
        allow_tools: Whether to allow tool calls (permission policy).
        cancel_check: Sync callable returning True if job should be cancelled.
        heartbeat: Sync callable for lease renewal.
        heartbeat_interval: Seconds between heartbeat calls.
        stderr_path: Optional path to write agent stderr.

    Returns:
        Result with status, output, and metadata.
    """
    import acp

    started = _now()
    client = _HeadlessClient(allow_tools=allow_tools)

    # Decide stderr destination: pipe to file if path given, otherwise PIPE (discarded)
    if stderr_path is not None:
        stderr_fd = open(stderr_path, "w")  # noqa: SIM115 — closed in finally below
        transport_kwargs: dict[str, Any] = {"stderr": stderr_fd.fileno()}
    else:
        stderr_fd = None
        transport_kwargs = {}

    session_id: str | None = None
    cancelled_flag = False

    # Inner coroutine wrapping handshake + prompt so that ONE outer wait_for
    # covers the entire flow.  A hung initialize/new_session (handshake phase)
    # would otherwise block the worker forever because only the prompt call
    # was previously guarded by a timeout.
    async def _run_with_timeout() -> Result:
        nonlocal session_id, cancelled_flag

        async with acp.spawn_agent_process(
            client,
            provider_executable,
            *provider_args,
            env=env,
            cwd=cwd,
            transport_kwargs=transport_kwargs,
        ) as (conn, _process):
            # Handshake — covered by the outer wait_for timeout
            await conn.initialize(acp.PROTOCOL_VERSION)
            session_resp = await conn.new_session(cwd=str(cwd))
            session_id = session_resp.session_id

            # Background tasks: cancel check and heartbeat
            stop_event = asyncio.Event()
            bg_tasks: list[asyncio.Task[None]] = []

            # Mutable container for the prompt Task — set before awaiting so
            # _cancel_watcher can cancel it directly when cancel fires.
            _prompt_task_holder: list[asyncio.Task[Any]] = []

            if cancel_check is not None:
                async def _cancel_watcher() -> None:
                    nonlocal cancelled_flag
                    while not stop_event.is_set():
                        # Call cancel_check() directly in the coroutine — NOT via
                        # asyncio.to_thread().  asyncio.run() creates the event loop
                        # on the worker (OS) thread; the SQLite Store is also created
                        # on that same thread.  to_thread() would dispatch to the
                        # default-executor pool (a *different* thread), causing a
                        # sqlite3.ProgrammingError: "SQLite objects created in a
                        # thread can only be used in that same thread."  The call is
                        # a quick DB read (< 1ms), matching how base.py's poller
                        # calls cancel_check synchronously every 0.5 s.
                        try:
                            should_cancel = cancel_check()
                        except sqlite3.ProgrammingError:
                            # Surface loudly — this means a threading invariant was
                            # broken and cancel checking is dead.
                            logger.warning(
                                "cancel_check raised ProgrammingError (SQLite thread "
                                "violation); cancel checking is non-functional",
                                exc_info=True,
                            )
                            should_cancel = False
                        except Exception:  # noqa: BLE001
                            logger.warning("cancel_check raised unexpectedly", exc_info=True)
                            should_cancel = False
                        if should_cancel:
                            cancelled_flag = True
                            stop_event.set()
                            # Best-effort ACP cancel notification to the agent
                            try:
                                await conn.cancel(session_id=session_id)
                            except Exception:  # noqa: BLE001
                                pass
                            # Also cancel the prompt Task directly — agents may not
                            # respond to conn.cancel() (e.g. stub no-ops it), so
                            # cancelling the Task is the guaranteed abort path.
                            if _prompt_task_holder and not _prompt_task_holder[0].done():
                                _prompt_task_holder[0].cancel()
                            return
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(asyncio.ensure_future(stop_event.wait())),
                                timeout=1.0,
                            )
                        except asyncio.TimeoutError:
                            pass
                bg_tasks.append(asyncio.create_task(_cancel_watcher()))

            if heartbeat is not None:
                async def _heartbeat_watcher() -> None:
                    while not stop_event.is_set():
                        try:
                            await asyncio.wait_for(
                                asyncio.shield(asyncio.ensure_future(stop_event.wait())),
                                timeout=heartbeat_interval,
                            )
                        except asyncio.TimeoutError:
                            # Same threading note as _cancel_watcher: call heartbeat()
                            # directly to stay on the worker thread and avoid SQLite
                            # ProgrammingError from to_thread().
                            try:
                                heartbeat()
                            except sqlite3.ProgrammingError:
                                logger.warning(
                                    "heartbeat raised ProgrammingError (SQLite thread "
                                    "violation); lease renewal is non-functional",
                                    exc_info=True,
                                )
                            except Exception:  # noqa: BLE001
                                logger.warning(
                                    "heartbeat raised unexpectedly", exc_info=True
                                )  # heartbeat failure must not affect the job
                bg_tasks.append(asyncio.create_task(_heartbeat_watcher()))

            # Run prompt as a Task so _cancel_watcher can abort it directly
            # when cancel fires (conn.cancel() is best-effort but agents may
            # not respond; cancelling the Task guarantees prompt() is aborted).
            prompt_task: asyncio.Task[Any] = asyncio.create_task(
                conn.prompt(
                    prompt=[acp.text_block(prompt)],
                    session_id=session_id,
                )
            )
            # Register in the holder so _cancel_watcher can reach it.
            _prompt_task_holder.append(prompt_task)

            try:
                prompt_resp = await prompt_task
                # Drain pending session_update notification tasks after prompt() resolves.
                #
                # The SDK dispatcher fire-and-forgets notification handler tasks;
                # the prompt response future resolves independently and before those
                # tasks are scheduled.  Without yielding, accumulated_text would be
                # read in a partially-accumulated state.
                #
                # Strategy: yield until quiescent — stop when two consecutive yields
                # produce no new chunks.  A hard cap (100 yields) guards against a
                # runaway agent that streams forever after prompt() returns.
                # TaskSupervisor.shutdown() cancels (not drains) in-flight tasks,
                # so there is no deterministic barrier; quiescence is the best proxy.
                _drain_cap = 100
                _prev_len = len(client._chunks)
                _consecutive_no_new = 0
                for _ in range(_drain_cap):
                    await asyncio.sleep(0)
                    _cur_len = len(client._chunks)
                    if _cur_len == _prev_len:
                        _consecutive_no_new += 1
                        if _consecutive_no_new >= 2:
                            break
                    else:
                        _consecutive_no_new = 0
                    _prev_len = _cur_len
            except asyncio.CancelledError:
                # prompt_task was cancelled by _cancel_watcher → treat as cancelled
                if cancelled_flag:
                    return Result(
                        status="cancelled",
                        exit_code=-1,
                        started_at=started,
                        finished_at=_now(),
                    )
                # Otherwise re-raise (outer wait_for timeout cancellation)
                raise
            finally:
                stop_event.set()
                if not prompt_task.done():
                    prompt_task.cancel()
                    try:
                        await prompt_task
                    except (asyncio.CancelledError, Exception):
                        pass
                for t in bg_tasks:
                    t.cancel()
                    try:
                        await t
                    except asyncio.CancelledError:
                        pass

            if cancelled_flag:
                return Result(
                    status="cancelled",
                    exit_code=-1,
                    started_at=started,
                    finished_at=_now(),
                )

            # Map stop_reason to herder Result status
            stop_reason = prompt_resp.stop_reason
            output = client.accumulated_text

            # Usage: best-effort — PromptResponse.usage is Optional
            usage: dict[str, Any] | None = None
            if prompt_resp.usage is not None:
                try:
                    usage = prompt_resp.usage.model_dump(exclude_none=True)
                except Exception:  # noqa: BLE001
                    usage = None

            if stop_reason == "end_turn":
                return Result(
                    status="done",
                    exit_code=0,
                    output=output,
                    usage=usage,
                    started_at=started,
                    finished_at=_now(),
                )

            if stop_reason == "cancelled":
                return Result(
                    status="cancelled",
                    exit_code=-1,
                    output=output,
                    usage=usage,
                    started_at=started,
                    finished_at=_now(),
                )

            # refusal, max_tokens, max_turn_requests → failed
            # Justification: these all represent the agent declining/exhausting capacity
            # to fulfil the request.  "bad_prompt" is the closest herder error_type
            # (the prompt caused the agent to refuse or exceed its budget).
            return Result(
                status="failed",
                exit_code=1,
                output=output,
                error_type="bad_prompt",
                usage=usage,
                started_at=started,
                finished_at=_now(),
            )

    try:
        # Wrap the entire handshake+prompt flow in ONE outer timeout so that a
        # hung initialize/new_session does not block the worker forever.
        # On TimeoutError: only cancel the session if one was established
        # (session_id is set after new_session returns).
        return await asyncio.wait_for(_run_with_timeout(), timeout=timeout)
    except asyncio.TimeoutError:
        # The process exits when spawn_agent_process's context manager unwinds
        # after CancelledError propagates through _run_with_timeout.  conn is
        # not accessible here; session_id may or may not have been set depending
        # on whether the timeout fired during handshake or prompt.
        return Result(
            status="timeout",
            exit_code=-1,
            error_type="timeout",
            started_at=started,
            finished_at=_now(),
        )
    except FileNotFoundError:
        # Executable not found
        return Result(
            status="failed",
            exit_code=127,
            error_type="unavailable",
            stderr=f"ACP agent executable not found: {provider_executable}",
            started_at=started,
            finished_at=_now(),
        )
    except Exception as exc:  # noqa: BLE001
        logger.debug("ACP provider error: %s", exc, exc_info=True)
        return Result(
            status="failed",
            exit_code=1,
            error_type="unknown",
            stderr=str(exc),
            started_at=started,
            finished_at=_now(),
        )
    finally:
        if stderr_fd is not None:
            try:
                stderr_fd.close()
            except Exception:  # noqa: BLE001
                pass


# ---------------------------------------------------------------------------
# Public sync entry point (mirrors cli_generic.run signature)
# ---------------------------------------------------------------------------

def run(
    provider: Any,
    prompt: str,
    *,
    cwd: Path,
    run_dir: Path,
    env: dict,
    timeout: int,
    stdout_path: Path | None = None,
    stderr_path: Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
    heartbeat: Callable[[], None] | None = None,
    heartbeat_interval: float = 30.0,
    sandbox_profile: str | None = None,
    allow_tools: bool = False,
) -> Result:
    """Execute an ACP provider synchronously.

    This is the sync entry point called by providers/run.py, mirroring the
    cli_generic.run() signature so the dispatcher can call it uniformly.

    asyncio.run() creates a fresh event loop per call.  queue_claim runs jobs
    on a ThreadPoolExecutor; each thread calls asyncio.run() independently —
    no shared loop, no cross-thread interference.

    Args:
        provider: Provider configuration (executable, args, timeout, etc.).
        prompt: Input prompt text.
        cwd: Working directory for the agent subprocess.
        run_dir: Directory for temporary files (unused by ACP v1, kept for API compat).
        env: Environment variables (herder-minimised; SDK baseline merged on top).
        timeout: Execution timeout in seconds.
        stdout_path: Unused by ACP (output captured in memory); kept for API compat.
        stderr_path: Optional path to save agent stderr.
        cancel_check: Sync callable returning True if job should be cancelled.
        heartbeat: Sync callable to renew job lease periodically.
        heartbeat_interval: Seconds between heartbeat calls (default 30.0).
        sandbox_profile: Must be None; ACP cannot be seatbelt-wrapped in v1.
        allow_tools: If True, allow agent tool-use requests (first allow_* option wins).
            Wire from supervisor: allow_tools = (perms.filesystem != "read_only").

    Returns:
        Result with status, output, and metadata.

    Raises:
        RuntimeError: If sandbox_profile is not None (defense in depth — the config
            guard in validate_refs() should prevent untrusted+acp at load time).
        RuntimeError: If the acp package is not installed.
    """
    # Lazy import — existing tests and CLI paths must not require acp to be installed
    try:
        import acp  # noqa: F401
    except ImportError as exc:
        raise RuntimeError(
            "ACP provider requires the optional dependency: "
            "uv pip install 'herder[acp]'"
        ) from exc

    # Defense in depth: ACP v1 cannot run inside seatbelt sandbox.
    # The config guard (validate_refs: untrusted+acp → ConfigError) prevents
    # this combination at load time, but we guard here too.
    if sandbox_profile is not None:
        raise RuntimeError(
            "ACP providers cannot be wrapped with a sandbox profile in v1. "
            "Do not assign ACP providers to untrusted roles."
        )

    executable = provider.executable or ""
    args = list(provider.args) if provider.args else []

    return asyncio.run(
        _run_async(
            executable,
            args,
            prompt,
            cwd=cwd,
            env=dict(env) if env else {},
            timeout=float(timeout),
            allow_tools=allow_tools,
            cancel_check=cancel_check,
            heartbeat=heartbeat,
            heartbeat_interval=heartbeat_interval,
            stderr_path=stderr_path,
        )
    )
