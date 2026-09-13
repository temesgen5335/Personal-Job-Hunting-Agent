"""One function moves an application through its lifecycle (R23).

The map is `core.schemas.ALLOWED_TRANSITIONS`; this module is the only code that
applies it. Before it existed the same eight lines were inlined in two API routes, and
a third caller (the MCP operator tool) would have made a third copy — which is how a
process rule turns into three slightly different process rules.

A correction (`correction=True`) is possible and never silent: it writes a
`status_correction` event carrying who asked (`source`) and why (`reason`). It only
counts as a correction when the move is actually outside the map; a legal move with the
flag set is just a legal move.
"""

from __future__ import annotations

from dataclasses import dataclass

from jobagent.core.schemas import ApplicationStatus, Event, allowed_next, can_transition

VALID_STATUSES: frozenset[str] = frozenset(s.value for s in ApplicationStatus)


class UnknownStatus(ValueError):
    """Not a status at all — distinct from a status that is not reachable from here."""


class NoSuchApplication(LookupError):
    pass


class IllegalTransition(Exception):
    """The move is outside the map and no correction was requested."""

    def __init__(self, current: str, target: str):
        self.current, self.target = current, target
        self.allowed = sorted(allowed_next(current))
        super().__init__(f"Cannot move {current} → {target}. "
                         f"Allowed: {', '.join(self.allowed) or 'none (terminal)'}.")


@dataclass(frozen=True)
class Transition:
    application_id: str
    job_id: str | None
    previous: str
    status: str
    allowed_next: tuple[str, ...]
    corrected: bool


def transition(store, application_id: str, target: str, *, correction: bool = False,
               source: str = "api", reason: str = "") -> Transition:
    """Move one application to `target`, or raise. Writes `status` only — never
    `approved_at` or `submitted_at`, which belong to the send path (R2)."""
    if target not in VALID_STATUSES:
        raise UnknownStatus(f"{target!r} is not a status. One of: {sorted(VALID_STATUSES)}")
    row = store.get_application(application_id)
    if not row:
        raise NoSuchApplication(application_id)
    current = row["status"]
    legal = can_transition(current, target)
    if not legal:
        if not correction:
            raise IllegalTransition(current, target)
        store.log_event(Event(kind="status_correction", job_id=row.get("job_id"), payload={
            "application_id": application_id, "from": current, "to": target,
            "source": source, "reason": reason,
        }))
    store.update_application(application_id, status=target)
    return Transition(application_id, row.get("job_id"), current, target,
                      tuple(sorted(allowed_next(target))), corrected=not legal)
