"""Dynamic Reroute Engine.

Mirrors the diagram's two scenarios:
  - ECG under maintenance -> safely move ECG later (clinically permitted).
  - X-Ray room closed -> CANNOT be moved (must_be_last). Reserve a slot.

Authority hierarchy:
  - `must_be_last` is HARD: a test in this set is never moved.
  - `reroute_permissions.can_move_later` is the OVERRIDE: when true, the
    engine may defer the unavailable test past its preferred position even if
    that relaxes a `must_precede` preference (the data handoff between the two
    tests still happens once both are done).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.knowledge import must_be_last, reroute_permission


@dataclass
class RerouteDecision:
    action: str  # "reordered" | "reserved_slot" | "no_change"
    new_sequence: list
    affected_test: str
    reserved_for_time: str | None = None
    reason: str = ""


def decide_reroute(
    current_sequence: list,
    current_index: int,
    unavailable_test: str,
    reserve_minutes_ahead: int = 90,
) -> RerouteDecision:
    """Decide what to do when `unavailable_test` is suddenly unavailable."""
    if unavailable_test not in current_sequence:
        return RerouteDecision(
            action="no_change",
            new_sequence=current_sequence,
            affected_test=unavailable_test,
            reason="Test not in patient's plan.",
        )

    # If the patient has already passed this test, nothing to do.
    if current_sequence.index(unavailable_test) < current_index:
        return RerouteDecision(
            action="no_change",
            new_sequence=current_sequence,
            affected_test=unavailable_test,
            reason="Already past this test.",
        )

    perm = reroute_permission(unavailable_test)
    must_last = must_be_last()

    # HARD: must_be_last tests cannot be moved.
    if unavailable_test in must_last or not perm.get("can_move_later", False):
        eta = datetime.now() + timedelta(minutes=reserve_minutes_ahead)
        return RerouteDecision(
            action="reserved_slot",
            new_sequence=current_sequence,
            affected_test=unavailable_test,
            reserved_for_time=eta.strftime("%I:%M %p").lstrip("0"),
            reason="Clinical chain must be preserved — slot reserved instead.",
        )

    # OVERRIDE: defer this test as late as possible while keeping must_be_last
    # tests at the end. Concretely: place the unavailable test just before
    # the first must_be_last test in the remaining plan.
    completed = current_sequence[:current_index]
    remaining = current_sequence[current_index:]
    others = [t for t in remaining if t != unavailable_test]

    insert_at = len(others)
    for i, t in enumerate(others):
        if t in must_last:
            insert_at = i
            break

    new_remaining = others[:insert_at] + [unavailable_test] + others[insert_at:]
    new_sequence = completed + new_remaining

    if new_sequence == current_sequence:
        return RerouteDecision(
            action="no_change",
            new_sequence=current_sequence,
            affected_test=unavailable_test,
            reason="No safe reorder produces a different plan.",
        )

    return RerouteDecision(
        action="reordered",
        new_sequence=new_sequence,
        affected_test=unavailable_test,
        reason="Deferred — clinical override permits this test to move later.",
    )
