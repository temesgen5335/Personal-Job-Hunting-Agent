"""Per-backend circuit breaker.

Without one, a dead provider is retried on every single call: the existing failover
loops the chain from the top each time, so a wrong API key costs a full round trip
forever. Here a failure has memory.

Shaped as a sibling of the project's existing HTTP retry helper — full-jitter backoff,
capped waits, and injected `now`/`rng` so tests are instant and deterministic rather
than timing real sleeps.

State is in-process only, deliberately. A restart is a legitimate "try again", and
persisting a PERMANENT verdict would outlive the fixed API key that caused it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from agentkit.llm.errors import Classified, Verdict

_TRANSIENT_THRESHOLD = 3        # consecutive failures before opening
_BASE_COOLDOWN_S = 60.0
_MAX_COOLDOWN_S = 900.0         # 15 min
_PERMANENT_S = float("inf")


@dataclass
class _State:
    open_until: float = 0.0
    consecutive: int = 0
    cooldown: float = _BASE_COOLDOWN_S
    last_reason: str = ""


@dataclass
class Breaker:
    """Tracks health per (provider, model)."""

    now: object = field(default=None)          # () -> float, injectable
    rng: object = field(default=None)          # () -> float in [0,1), injectable
    _states: dict[tuple[str, str], _State] = field(default_factory=dict)

    def __post_init__(self):
        if self.now is None:
            import time
            self.now = time.monotonic
        if self.rng is None:
            self.rng = random.random

    def _state(self, key: tuple[str, str]) -> _State:
        return self._states.setdefault(key, _State())

    def is_open(self, provider: str, model: str) -> bool:
        st = self._states.get((provider, model))
        return bool(st and st.open_until > self.now())

    def opens_in(self, provider: str, model: str) -> float:
        st = self._states.get((provider, model))
        return max(0.0, st.open_until - self.now()) if st else 0.0

    def reason(self, provider: str, model: str) -> str:
        st = self._states.get((provider, model))
        return st.last_reason if st else ""

    def record_success(self, provider: str, model: str) -> None:
        """Closes the breaker and resets the escalating cooldown — a half-open trial
        that works means the provider is genuinely back."""
        st = self._state((provider, model))
        st.consecutive = 0
        st.open_until = 0.0
        st.cooldown = _BASE_COOLDOWN_S
        st.last_reason = ""

    def record_failure(self, provider: str, model: str, c: Classified) -> None:
        st = self._state((provider, model))
        st.consecutive += 1
        st.last_reason = f"{c.verdict}: {c.message[:80]}"
        now = self.now()

        if c.verdict is Verdict.PERMANENT:
            # A wrong key will still be wrong in 60s. Stay shut until the config
            # changes — the host calls reset() when it reloads settings.
            st.open_until = _PERMANENT_S
        elif c.verdict is Verdict.RATE_LIMIT:
            # Honor the server's own number when it gave one; it knows better than we do.
            st.open_until = now + (c.retry_after_s if c.retry_after_s is not None
                                   else self._jittered(st))
        elif c.verdict is Verdict.TRANSIENT or c.verdict is Verdict.UNKNOWN:
            if st.consecutive >= _TRANSIENT_THRESHOLD:
                st.open_until = now + self._jittered(st)
                st.cooldown = min(_MAX_COOLDOWN_S, st.cooldown * 2)
        # BAD_REQUEST / CAPABILITY / CONTEXT / CONTENT_FILTER are about THIS request,
        # not the backend's health — opening on them would disable a working provider
        # because of one malformed prompt.

    def _jittered(self, st: _State) -> float:
        """Full jitter, so several backends failing together don't retry in lockstep."""
        return st.cooldown * self.rng()

    def reset(self, provider: str | None = None, model: str | None = None) -> None:
        """Clear breaker state. Call after a config reload: new credentials deserve a
        fresh chance, and a PERMANENT verdict would otherwise outlive the fixed key."""
        if provider is None:
            self._states.clear()
        elif model is not None:
            self._states.pop((provider, model), None)
        else:
            for key in [k for k in self._states if k[0] == provider]:
                del self._states[key]

    def snapshot(self) -> dict[str, str]:
        """Human-readable health, for a diagnostic command."""
        out = {}
        for (provider, model), st in self._states.items():
            if st.open_until > self.now():
                left = "permanently" if st.open_until == _PERMANENT_S else \
                    f"for {st.open_until - self.now():.0f}s"
                out[f"{provider}/{model}"] = f"open {left} — {st.last_reason}"
        return out
