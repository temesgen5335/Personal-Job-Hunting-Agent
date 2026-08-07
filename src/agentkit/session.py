"""What a policy decision is allowed to know.

This file is short because its value is what it *lacks*. `SessionContext` carries no
transcript, no retrieved chunks, and no model output — only the operator's identity,
the surface they are on, and what they have already approved.

That absence is the structural defense against retrieved text influencing a permission
decision. Fences and provenance labels reduce the odds a model is talked into asking
for something; they cannot make it impossible. So the thing that answers "may this
run?" is given no access to the text that might be doing the talking. A prompt
injection can make the model *request* a config rewrite. It cannot make the gatekeeper
approve one, because the gatekeeper cannot read it.

A test asserts the absence, since the natural direction of drift is for someone to add
`transcript` here to improve a log message.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Surface(StrEnum):
    """Where the operator is. Not cosmetic: a one-tap approval on a phone with no
    re-auth is a materially weaker signal than the same click on an authenticated
    dashboard, so the host may restrict ADMIN confirmations to a surface."""

    CLI = "cli"
    WEB = "web"
    CHAT = "chat"


@dataclass(frozen=True)
class SessionContext:
    """Everything a policy decision may consider.

    Deliberately absent — and asserted absent by test:
      transcript, messages, retrieved chunks, model output, tool results.
    """

    actor: str = "operator"
    surface: Surface = Surface.CLI
    run_id: str = ""
    # Surfaces on which ADMIN actions may be confirmed at all. Empty = any surface.
    admin_surfaces: frozenset[Surface] = field(default_factory=frozenset)

    def may_confirm_admin(self) -> bool:
        return not self.admin_surfaces or self.surface in self.admin_surfaces
