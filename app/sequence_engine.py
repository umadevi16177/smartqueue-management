"""Medical Sequence Engine.

Locks clinical order *first*, before any queue/availability data is consulted.
Reads from Clinical Rules Store (knowledge.py) and produces a deterministic
ordering. The diagram's hero rule:

    Blood Test -> ECG -> Ultrasound -> X-Ray
"""
from __future__ import annotations

from app.knowledge import (
    clinical_rules,
    must_be_last,
    precedence_pairs,
)


class SequencingError(ValueError):
    pass


def sequence_tests(requested: list[str]) -> list[str]:
    """Return the clinically correct order for the requested test codes.

    Algorithm: topological sort over precedence pairs, with `must_be_last`
    constraint enforced after the sort. Within ties, fall back to the
    canonical_order from the rules file so output is deterministic.
    """
    if not requested:
        return []

    canonical = clinical_rules()["canonical_order"]
    canon_index = {code: i for i, code in enumerate(canonical)}

    # Validate every requested code is known to the canonical order.
    unknown = [c for c in requested if c not in canon_index]
    if unknown:
        raise SequencingError(f"Unknown test codes: {unknown}")

    requested_set = set(requested)
    # Build dependency graph restricted to requested tests.
    deps: dict[str, set[str]] = {c: set() for c in requested}
    for before, after in precedence_pairs():
        if before in requested_set and after in requested_set:
            deps[after].add(before)

    # Kahn's algorithm with deterministic tie-break by canonical index.
    ordered: list[str] = []
    remaining = dict(deps)
    while remaining:
        ready = [c for c, d in remaining.items() if not d]
        if not ready:
            raise SequencingError("Cycle in clinical rules — cannot sequence.")
        ready.sort(key=lambda c: canon_index[c])
        chosen = ready[0]
        ordered.append(chosen)
        del remaining[chosen]
        for d in remaining.values():
            d.discard(chosen)

    # Enforce must-be-last (e.g. X-Ray).
    last_set = must_be_last() & requested_set
    if last_set:
        non_last = [c for c in ordered if c not in last_set]
        last_in_canonical = sorted(last_set, key=lambda c: canon_index[c])
        ordered = non_last + last_in_canonical

    return ordered
