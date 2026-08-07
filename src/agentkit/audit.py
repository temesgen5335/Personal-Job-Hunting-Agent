"""The record of what the agent did, written before it does it.

Two decisions that look pedantic and are not:

**Intent is recorded before the policy runs.** Not after the decision, not after the
call. If the trail only contains permitted actions, it answers "what did the agent do"
but not "what did it try to do" — and the second question is the one you ask after
something goes wrong. A refused attempt to rewrite configuration is the single most
interesting line the log can contain, and it is exactly the line an after-the-fact
trail discards.

**Audit failure aborts the call.** If the sink raises, the tool does not run. An agent
that keeps acting while its trail is broken is an agent whose actions cannot be
reconstructed, which is worse than an agent that stopped.

The sink itself is the host's — this module defines the shape and the discipline, not
the storage. A session is a run: it mints a run id, emits spans under it, and closes
with a summary, so agent activity lands in whatever run ledger the host already has.
"""

from __future__ import annotations

import secrets
from dataclasses import dataclass, field
from typing import Protocol

# Arguments can carry text the operator typed. The trail records shape and a digest,
# never the payload — a log is a place data leaks to.
PREVIEW_CHARS = 60


class AuditSink(Protocol):
    """The host's storage. One method, so implementing it against an existing events
    table is a few lines."""

    def emit(self, kind: str, payload: dict) -> None:
        ...


class AuditUnavailable(RuntimeError):
    """The trail could not be written, so the action must not proceed."""


def new_run_id() -> str:
    return secrets.token_hex(6)


def preview(value) -> str:
    """A short, safe rendering of an argument value.

    Length is kept because "the model passed a 40kb string" is worth knowing; content
    is not, because a log is read by more people and processes than the thing it logs.
    """
    text = str(value)
    if len(text) <= PREVIEW_CHARS:
        return text
    return f"{text[:PREVIEW_CHARS]}…(+{len(text) - PREVIEW_CHARS} chars)"


def redact_args(args: dict) -> dict:
    return {k: preview(v) for k, v in (args or {}).items()}


@dataclass
class Auditor:
    """Writes one session's trail. Fail-closed by construction."""

    sink: AuditSink | None = None
    run_id: str = field(default_factory=new_run_id)
    actor: str = "agent"
    emitted: int = 0
    failures: int = 0

    def _write(self, kind: str, payload: dict) -> None:
        if self.sink is None:
            # No sink configured is a legitimate mode (a CLI dry run); a sink that
            # exists and fails is not. Only the second one is fatal.
            return
        try:
            self.sink.emit(kind, {"run_id": self.run_id, "actor": self.actor, **payload})
            self.emitted += 1
        except Exception as exc:  # noqa: BLE001 — the sink is the host's code
            self.failures += 1
            raise AuditUnavailable(
                f"audit sink failed ({type(exc).__name__}: {exc}); "
                f"refusing to proceed without a trail") from exc

    # --- the three spans of a tool call --------------------------------------------

    def intent(self, tool: str, args: dict) -> None:
        """Before the policy runs. This is the line that survives a refusal."""
        self._write("tool_intent", {"tool": tool, "args": redact_args(args)})

    def decision(self, tool: str, decision: str, reason: str) -> None:
        self._write("tool_decision",
                    {"tool": tool, "decision": decision, "reason": reason})

    def result(self, tool: str, *, ok: bool, size: int, elapsed_ms: int = 0) -> None:
        """Results are recorded as a byte count. What a tool returned can be
        reconstructed by running it again; what it leaked into a log cannot be
        un-leaked."""
        self._write("tool_result",
                    {"tool": tool, "ok": ok, "bytes": size, "elapsed_ms": elapsed_ms})

    def note(self, kind: str, **payload) -> None:
        """A host-defined line on the same spine."""
        self._write(kind, payload)

    def close(self, *, summary: str = "", **extra) -> None:
        """The closing `run` line, so a session appears in the host's run ledger
        alongside its scheduled work with no new storage."""
        self._write("run", {"kind_detail": "agent_session", "summary": summary,
                            "tool_calls": self.emitted, **extra})
