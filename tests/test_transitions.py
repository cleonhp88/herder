"""Pure FSM unit tests for herder.transitions.

Tests cover is_legal and assert_transition for representative legal and
illegal pairs without touching a database.
"""

from __future__ import annotations

import pytest

from herder.transitions import (
    LEGAL_TRANSITIONS,
    IllegalTransitionError,
    assert_transition,
    is_legal,
)

# ── is_legal — legal pairs ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("running", "done"),
        ("running", "failed"),
        ("running", "cancelled"),
        ("running", "cancelling"),
        ("running", "running"),  # lease-reclaim
        ("cancelling", "done"),
        ("cancelling", "failed"),
        ("cancelling", "cancelled"),
        ("failed", "pending"),  # auto-retry requeue
        ("failed", "dead"),  # mark_dead after exhaustion
        ("dead", "pending"),  # manual CLI retry
        ("cancelled", "pending"),  # manual CLI retry
        ("pending", "running"),
        ("pending", "cancelled"),
        ("waiting_approval", "approved"),
        ("waiting_approval", "rejected"),
        ("approved", "running"),
    ],
)
def test_is_legal_true(from_status: str, to_status: str) -> None:
    """is_legal returns True for all expected legal transitions."""
    assert is_legal(from_status, to_status) is True


# ── is_legal — illegal pairs ─────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("from_status", "to_status"),
    [
        ("done", "pending"),  # THE critical guard: resurrection forbidden
        ("pending", "done"),  # cannot skip running
        ("running", "pending"),  # cannot go backwards
        ("dead", "running"),  # only pending is legal from dead
        ("rejected", "pending"),  # rejected is terminal
        ("done", "failed"),  # done is terminal
        ("done", "running"),  # done is terminal
    ],
)
def test_is_legal_false(from_status: str, to_status: str) -> None:
    """is_legal returns False for all expected illegal transitions."""
    assert is_legal(from_status, to_status) is False


# ── assert_transition — raises on illegal ────────────────────────────────────


def test_assert_transition_legal_does_not_raise() -> None:
    """assert_transition does not raise for a legal transition."""
    assert_transition("running", "done")  # must not raise


def test_assert_transition_illegal_raises() -> None:
    """assert_transition raises IllegalTransitionError for done → pending."""
    with pytest.raises(IllegalTransitionError):
        assert_transition("done", "pending")


def test_assert_transition_message_contains_statuses() -> None:
    """IllegalTransitionError message includes both from and to statuses."""
    with pytest.raises(IllegalTransitionError, match=r"running.*pending"):
        assert_transition("running", "pending")


def test_assert_transition_reason_in_message() -> None:
    """reason kwarg is appended to the error message."""
    with pytest.raises(IllegalTransitionError, match=r"reason=requeue"):
        assert_transition("done", "pending", reason="requeue")


def test_assert_transition_unknown_from_status_raises() -> None:
    """assert_transition raises for an unknown from_status (not in FSM)."""
    with pytest.raises(IllegalTransitionError):
        assert_transition("bogus_status", "pending")


# ── LEGAL_TRANSITIONS structural invariants ──────────────────────────────────


def test_terminals_have_empty_transition_sets() -> None:
    """done and rejected are true terminals — no outgoing edges."""
    assert LEGAL_TRANSITIONS["done"] == frozenset()
    assert LEGAL_TRANSITIONS["rejected"] == frozenset()


def test_all_values_are_frozensets() -> None:
    """Every value in LEGAL_TRANSITIONS is a frozenset (not a set or list)."""
    for key, val in LEGAL_TRANSITIONS.items():
        assert isinstance(val, frozenset), f"{key!r} value is not a frozenset"
