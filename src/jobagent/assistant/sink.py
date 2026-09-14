"""One audit sink for every surface.

Four renderers — CLI, HTTP, Telegram, MCP — write one trail. Before this module each
kept its own three-line `EventSink`; a fourth copy is the point at which one drifts.
This is the host's half of `agentkit.audit.AuditSink`: the agent's spans land on the
same `events` table as the pipeline's, so a session shows up in `GET /runs` beside the
scheduled work with no new storage.
"""

from __future__ import annotations

from jobagent.core.schemas import Event


class StoreSink:
    """Writes each audit line as an `Event`. If this raises, the tool call is aborted
    before the policy is consulted (R27) — that is the Auditor's job, not ours."""

    def __init__(self, store):
        self.store = store

    def emit(self, kind: str, payload: dict) -> None:
        self.store.log_event(Event(kind=kind, payload=payload))
