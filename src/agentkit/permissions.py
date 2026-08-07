"""Who may do what, and what must be confirmed first.

Two orthogonal axes, because conflating them produces the wrong answer in both
directions:

- **permission** — how much of the world the tool touches (READ / ACT / ADMIN)
- **cost** — whether it burns a metered resource

A tool can read nothing and still cost real money on every call, and a tool can change
state cheaply. Governing cost with a confirmation prompt trains the operator to click
through; governing it with a counter does not.

The fourth category has no enum member on purpose. **EXCLUDED means the tool does not
exist.** A gate is a runtime check an attacker has to defeat once; absence is
structural, and `PolicyBook.guard()` turns a registration attempt into a build-time
error. That is why `excluded` is a set of names rather than a `Permission` value —
there is no code path in which an excluded tool is looked up, permitted, and run.

Confirmation is deliberately awkward to fake:

- the nonce is minted server-side and never derived from anything the model emits
- it is bound to `sha256` of the exact arguments, so confirm-then-swap fails
- it is single-use and expires

The last two matter because the interesting attack is not "get a dangerous tool
approved", it is "get a harmless one approved and then change what it does".
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum

# Long enough for a human to read a diff and decide; short enough that an approval
# cannot be replayed hours later against changed state.
NONCE_TTL_S = 300.0


# Names no host may ever register, folded into every PolicyBook automatically.
#
# These are not dangerous because of what they do — they are dangerous because they are
# *general*. A tool that runs arbitrary SQL, shell, or HTTP collapses every other
# restriction in this module into a suggestion: excluded tools become reachable, frozen
# config becomes writable, and the audit trail records one opaque line instead of the
# action that actually happened. There is no permission tier at which that is
# acceptable, so there is no tier where it is offered.
#
# Hosts should expose narrow, named operations instead — `set_max_age(days)` can be
# reviewed on a confirmation card; `execute_sql(query)` cannot.
UNIVERSALLY_EXCLUDED = frozenset({
    "execute_sql", "run_sql", "query", "raw_query",
    "run_shell", "shell", "exec", "execute", "subprocess", "eval",
    "http_fetch", "fetch_url", "request", "curl",
    "write_file", "delete_file", "read_file", "filesystem", "open_file",
})


class Permission(IntEnum):
    """Ordered so a grant check is a comparison. There is no EXCLUDED member — see the
    module docstring."""

    READ = 0        # observes state, changes nothing
    ACT = 1         # changes application state
    ADMIN = 2       # changes configuration — the thing that changes everything else


class Confirm(StrEnum):
    NEVER = "never"
    SESSION = "session"     # approve once, then trusted for this session
    ALWAYS = "always"       # approve every single time


class Decision(StrEnum):
    ALLOW = "allow"
    CONFIRM = "confirm"
    DENY = "deny"


@dataclass(frozen=True)
class ToolPolicy:
    name: str
    permission: Permission = Permission.READ
    confirm: Confirm = Confirm.NEVER
    # Orthogonal to permission: no world-effect, but a metered resource.
    costly: bool = False
    # Free text shown on the confirmation card, from the host — never from the model.
    describes: str = ""


@dataclass(frozen=True)
class Outcome:
    decision: Decision
    reason: str
    policy: ToolPolicy | None = None

    @property
    def allowed(self) -> bool:
        return self.decision is Decision.ALLOW


class ExcludedTool(Exception):
    """Raised when something tries to register a structurally forbidden tool. This is
    a programming error, surfaced at wiring time rather than at run time."""


def args_digest(args: dict) -> str:
    """A stable fingerprint of a call's arguments.

    `sort_keys` matters: two dicts that mean the same thing must fingerprint the same,
    or a confirmation would spuriously fail on key ordering. `default=str` keeps a
    non-JSON value from raising inside a security check.
    """
    blob = json.dumps(args or {}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(blob.encode()).hexdigest()


@dataclass(frozen=True)
class Pending:
    """One outstanding confirmation. Lives server-side; the model never sees it."""

    nonce: str
    tool: str
    digest: str
    expires_at: float
    describes: str = ""


@dataclass
class PolicyBook:
    """The host's declaration of what its tools are allowed to do.

    Unknown tools default to DENY rather than READ. A tool that someone forgot to
    declare is exactly the tool whose blast radius nobody has thought about.
    """

    policies: dict[str, ToolPolicy] = field(default_factory=dict)
    excluded: frozenset[str] = frozenset()
    # Ceiling for `costly` tools per session. None = uncounted.
    cost_budget: int | None = None

    def __post_init__(self):
        # Union rather than default, so a host that passes its own exclusions cannot
        # accidentally drop the universal ones by overwriting the field.
        self.excluded = frozenset(self.excluded) | UNIVERSALLY_EXCLUDED

    def declare(self, policy: ToolPolicy) -> None:
        if policy.name in self.excluded:
            raise ExcludedTool(f"{policy.name!r} is excluded and cannot be declared")
        self.policies[policy.name] = policy

    def guard(self, name: str) -> None:
        """Called at tool registration. Turns 'this tool must never exist' from a
        convention into an exception."""
        if name in self.excluded:
            raise ExcludedTool(
                f"{name!r} is structurally excluded from this agent. It is not gated — "
                f"it must not be registered at all.")

    def policy_for(self, name: str) -> ToolPolicy | None:
        return self.policies.get(name)


@dataclass
class Gatekeeper:
    """Decides, mints confirmations, and redeems them.

    Holds no transcript and no retrieved text — see `SessionContext`. Everything it
    needs is the tool name, the validated arguments, and what the operator has already
    granted.
    """

    book: PolicyBook
    now: object = None                       # () -> float, injected in tests
    _pending: dict[str, Pending] = field(default_factory=dict)
    granted: set[str] = field(default_factory=set)      # SESSION approvals
    spent: int = 0                                       # costly calls made

    def __post_init__(self):
        if self.now is None:
            import time
            self.now = time.monotonic

    def decide(self, name: str, args: dict) -> Outcome:
        if name in self.book.excluded:
            # Belt and braces. Registration should have made this unreachable.
            return Outcome(Decision.DENY, f"{name!r} is excluded from this agent")

        policy = self.book.policy_for(name)
        if policy is None:
            return Outcome(Decision.DENY,
                           f"{name!r} has no declared policy; undeclared tools are denied")

        if policy.costly and self.book.cost_budget is not None \
                and self.spent >= self.book.cost_budget:
            return Outcome(Decision.DENY,
                           f"budget for metered tools spent ({self.book.cost_budget})",
                           policy)

        if policy.confirm is Confirm.NEVER:
            return Outcome(Decision.ALLOW, "no confirmation required", policy)

        if policy.confirm is Confirm.SESSION and name in self.granted:
            return Outcome(Decision.ALLOW, "approved earlier this session", policy)

        # ALWAYS never consults `granted`: a standing approval is exactly what an
        # operator should not be able to give to a config rewrite.
        return Outcome(Decision.CONFIRM, f"{policy.permission.name.lower()} action "
                                         f"requires approval", policy)

    def request(self, name: str, args: dict) -> Pending:
        """Mint a confirmation bound to these exact arguments."""
        policy = self.book.policy_for(name)
        pending = Pending(
            nonce=secrets.token_urlsafe(24),
            tool=name,
            digest=args_digest(args),
            expires_at=self.now() + NONCE_TTL_S,
            describes=policy.describes if policy else "",
        )
        self._pending[pending.nonce] = pending
        return pending

    def redeem(self, nonce: str, name: str, args: dict) -> Outcome:
        """Consume a confirmation. Every failure mode is a DENY, never a warning."""
        pending = self._pending.pop(nonce, None)     # single-use: popped on sight
        if pending is None:
            return Outcome(Decision.DENY, "unknown or already-used confirmation")
        if pending.expires_at <= self.now():
            return Outcome(Decision.DENY, "confirmation expired")
        if pending.tool != name:
            return Outcome(Decision.DENY, "confirmation was issued for a different tool")
        if pending.digest != args_digest(args):
            # The confirm-then-swap attack: approve something harmless, then run it with
            # different arguments. The binding is what makes the approval meaningful.
            return Outcome(Decision.DENY, "arguments changed since the confirmation")

        policy = self.book.policy_for(name)
        if policy is not None and policy.confirm is Confirm.SESSION:
            self.granted.add(name)
        return Outcome(Decision.ALLOW, "confirmed", policy)

    def note_spend(self, name: str) -> None:
        if (policy := self.book.policy_for(name)) is not None and policy.costly:
            self.spent += 1

    def purge_expired(self) -> int:
        stale = [n for n, p in self._pending.items() if p.expires_at <= self.now()]
        for n in stale:
            del self._pending[n]
        return len(stale)
