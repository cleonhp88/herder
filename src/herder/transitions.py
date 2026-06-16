"""Legal job-status FSM — single source of truth for transition guards.

Pure logic module with no DB dependency.  Import ``assert_transition``
from here; store mutators call it before executing any UPDATE.
"""

from __future__ import annotations

from herder.db.migrations import StoreError

# Full legal job-status FSM.
# Key   = from_status
# Value = frozenset of statuses reachable from that source.
#
# "running → running" is legal: worker lease-reclaim (claim_job on an
# expired-running row) re-enters the running state.
LEGAL_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "cancelled"}),
    "waiting_approval": frozenset({"approved", "rejected", "cancelled"}),
    "approved": frozenset({"running", "cancelled"}),
    "running": frozenset({"done", "failed", "cancelled", "cancelling", "running"}),
    "cancelling": frozenset({"done", "failed", "cancelled"}),
    "failed": frozenset({"pending", "dead"}),
    "dead": frozenset({"pending"}),  # manual CLI retry only
    "cancelled": frozenset({"pending"}),  # manual CLI retry only
    "done": frozenset(),  # terminal — no exits
    "rejected": frozenset(),  # terminal — no exits
}


class IllegalTransitionError(StoreError):
    """Raised when a job status transition is not in LEGAL_TRANSITIONS."""


def is_legal(from_status: str, to_status: str) -> bool:
    """Return True iff from_status → to_status is a legal FSM edge.

    Args:
        from_status: Current job status.
        to_status: Desired next job status.

    Returns:
        True if the transition is permitted, False otherwise.
    """
    return to_status in LEGAL_TRANSITIONS.get(from_status, frozenset())


def assert_transition(
    from_status: str,
    to_status: str,
    *,
    reason: str = "",
) -> None:
    """Raise IllegalTransitionError if from_status → to_status is not legal.

    Args:
        from_status: Current job status.
        to_status: Desired next job status.
        reason: Optional context string appended to the error message.

    Raises:
        IllegalTransitionError: If the transition is not in LEGAL_TRANSITIONS.
    """
    if not is_legal(from_status, to_status):
        suffix = f" (reason={reason})" if reason else ""
        raise IllegalTransitionError(
            f"illegal transition {from_status} -> {to_status}{suffix}"
        )
