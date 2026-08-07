"""Which settings the agent may change, and what changing them would do.

The user's explicit choice was "read + full actions including config", so the question
is not whether the agent touches configuration but how a mistake is bounded. Three
mechanisms, in descending order of how much they matter:

**Frozen is the complement, not a list.** `CONFIG_WRITABLE` names what may change;
everything else in `MANAGED_FIELDS` is frozen by construction. A setting added next
year is frozen the day it is added, without anyone remembering to add it here. A frozen
*list* would have the opposite default, and the failure would be silent.

**The dangerous fields are dangerous for a specific reason.** `custom_llm_base_url` is
the whole egress story: an agent that can write it redirects every future prompt — CV,
contact details, search history — to an endpoint of the attacker's choosing, *and*
makes every future model response attacker-authored. Freezing it means a fully
hijacked model has nowhere to send anything. `telegram_channels` is the same class from
the other end: writing it installs a persistent input inlet, which closes the loop into
a self-sustaining compromise. Neither is a hypothetical about a clever prompt; both are
one-line changes with permanent effect.

**A proposal is shown as a computed diff, never as model prose.** The impact preview is
produced by running the proposed filter over real stored data — "would have dropped 481
of 612" — so the operator approves an arithmetic fact, not a sentence the model wrote
about what it intends. And two outcomes are refused outright rather than confirmed,
because there is no version of them the operator meant.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from jobagent.secrets_store import MANAGED_FIELDS, SECRET_FIELDS

# The allow-list. Deliberately short: search behaviour and which model answers, nothing
# that decides where data goes or who may talk to the system.
CONFIG_WRITABLE: frozenset[str] = frozenset({
    # Ingest gate — what gets fetched and stored. Reversible, and the impact is
    # computable against stored history, which is what makes it safe to delegate.
    "ingest_max_age_days", "ingest_locations", "ingest_drop_keywords", "ingest_sources",
    # Which model answers. Changing these cannot move data anywhere new — every
    # provider endpoint is a constant in the chain table, not a setting.
    "llm_provider",
    "groq_model", "openrouter_model", "openai_model", "gemini_model",
    "anthropic_model", "qwen_model",
})

# Everything else the config UI manages. Computed, never typed out.
FROZEN: frozenset[str] = frozenset(MANAGED_FIELDS) - CONFIG_WRITABLE

# Named explicitly in the invariant test so the assertion reads as intent rather than
# as arithmetic. If one of these ever becomes writable it should be a loud diff.
MUST_STAY_FROZEN: frozenset[str] = frozenset({
    "custom_llm_base_url", "custom_llm_api_key",
    "telegram_channels", "telegram_chat_id", "telegram_bot_token", "telegram_api_hash",
    "telegram_api_id", "telegram_phone",
    "smtp_host", "smtp_port", "smtp_user", "smtp_password", "apply_from_email",
})


class ConfigRefused(Exception):
    """The proposal is not something to confirm — it is something to reject."""


@dataclass
class Impact:
    """What a change would actually do, computed from real data."""

    field: str
    current: str
    proposed: str
    summary: str
    detail: str = ""
    warnings: tuple[str, ...] = ()

    def render(self) -> str:
        """The confirmation card's body. Built from validated arguments and computed
        numbers only — no model output reaches this string."""
        lines = [f"{self.field}: {self.current or '(unset)'} → {self.proposed or '(unset)'}",
                 self.summary]
        if self.detail:
            lines.append(self.detail)
        lines += [f"WARNING: {w}" for w in self.warnings]
        return "\n".join(lines)


def check_writable(field_name: str) -> None:
    """Raise unless this field is on the allow-list. Message says *why*, because the
    operator reading it is the one who has to decide whether the rule is wrong."""
    if field_name in CONFIG_WRITABLE:
        return
    if field_name in SECRET_FIELDS:
        raise ConfigRefused(
            f"{field_name!r} is a credential. The agent never handles secret values; "
            f"set it yourself in the dashboard or .env.")
    if field_name in FROZEN:
        raise ConfigRefused(
            f"{field_name!r} is frozen: it controls where data goes or who may reach "
            f"the system, so it is not delegable. Change it yourself in the dashboard.")
    raise ConfigRefused(f"{field_name!r} is not a managed setting.")


# --- impact previews ----------------------------------------------------------------

def _as_posting(row: dict):
    """Rehydrate a stored row into the JobPosting the gate expects.

    The preview must run the *same* `IngestGate.reject()` the pipeline runs, or it is
    describing a filter that does not exist. Reimplementing the predicate against dicts
    would drift the moment the gate changes — which is exactly when an operator would
    most want the preview to be right.
    """
    import json

    from jobagent.core.schemas import JobPosting

    try:
        tags = row.get("tags")
        if isinstance(tags, str):
            tags = json.loads(tags or "[]")
        return JobPosting(
            id=row.get("id"),
            source=row.get("source") or "remoteok",
            title=row.get("title") or "",
            company=row.get("company"),
            location=row.get("location"),
            is_remote=bool(row.get("is_remote")),
            description=row.get("description") or "",
            url=row.get("url"),
            posted_at=row.get("posted_at") or None,
            tags=list(tags or []),
        )
    except Exception:  # noqa: BLE001 — a malformed row must not break a preview
        return None


def _gate_impact(field_name: str, value: str, settings, store) -> Impact:
    """Dry-run the proposed gate over the last 7 days of stored postings.

    This is the mechanism the whole design leans on: the operator is not asked to
    imagine what a filter would do, they are shown what it *did* do to real rows.
    """
    from jobagent.ingestion.gate import IngestGate

    current = str(getattr(settings, field_name, "") or "")
    overrides = {f: str(getattr(settings, f, "") or "") for f in
                 ("ingest_max_age_days", "ingest_locations", "ingest_drop_keywords")}
    overrides[field_name] = str(value)

    proposed = IngestGate(
        max_age_days=int(overrides["ingest_max_age_days"] or 0),
        locations=[s.strip() for s in overrides["ingest_locations"].split(",") if s.strip()],
        drop_keywords=[s.strip() for s in overrides["ingest_drop_keywords"].split(",") if s.strip()],
    )

    cutoff = (datetime.now(UTC) - timedelta(days=7)).isoformat()
    sample = [j for j in store.get_jobs(limit=2000)
              if (j.get("last_seen_at") or j.get("posted_at") or "") >= cutoff]
    if not sample:
        sample = store.get_jobs(limit=300)      # young store: use whatever exists

    reasons: dict[str, int] = {}
    dropped = 0
    for row in sample:
        posting = _as_posting(row)
        if posting is None:
            continue        # unreadable row: not evidence either way, so don't count it
        why = proposed.reject(posting)
        if why:
            dropped += 1
            reasons[why] = reasons.get(why, 0) + 1
    total = len(sample)

    warnings = []
    if total and dropped == total:
        # Not a confirmation candidate. Nobody means "filter out everything".
        raise ConfigRefused(
            f"that filter drops all {total} postings in the recent sample — "
            f"the pipeline would store nothing. Refusing rather than confirming.")
    if total and dropped / total > 0.9:
        warnings.append(f"drops {dropped / total:.0%} of recent postings")

    return Impact(
        field=field_name, current=current, proposed=str(value),
        summary=f"Would have dropped {dropped} of {total} postings seen recently.",
        detail="; ".join(f"{k}: {v}" for k, v in sorted(reasons.items())),
        warnings=tuple(warnings),
    )


def _llm_impact(field_name: str, value: str, settings, store) -> Impact:
    """Show the chain the change would produce, and refuse one that empties it."""
    from agentkit.llm.chain import build_chain

    current = str(getattr(settings, field_name, "") or "")
    proposed_settings = settings.model_copy(update={field_name: value})
    report = build_chain(proposed_settings, report=True)

    if not report.backends:
        raise ConfigRefused(
            f"setting {field_name}={value!r} leaves no usable model — every provider "
            f"would be skipped ({'; '.join(f'{n}: {w}' for n, w in report.skipped)}). "
            f"Refusing rather than confirming.")

    return Impact(
        field=field_name, current=current, proposed=str(value),
        summary=f"Resulting chain: {len(report.backends)} usable provider(s).",
        detail=" → ".join(f"{b.name}/{b.model}" for b in report.backends),
        warnings=tuple(f"skipped {n}: {w}" for n, w in report.skipped),
    )


def preview(field_name: str, value: str, settings, store) -> Impact:
    """Compute what a proposed change would do. Raises `ConfigRefused` if it should
    never be offered at all."""
    check_writable(field_name)
    if field_name.startswith("ingest_") and field_name != "ingest_sources":
        return _gate_impact(field_name, value, settings, store)
    if field_name == "ingest_sources":
        from jobagent.ingestion.gate import ALL_SOURCES
        chosen = [s.strip() for s in str(value).split(",") if s.strip()]
        unknown = [s for s in chosen if s not in ALL_SOURCES]
        if unknown:
            raise ConfigRefused(f"unknown source(s): {unknown}. Known: {sorted(ALL_SOURCES)}")
        if not chosen:
            raise ConfigRefused("an empty source list would fetch nothing.")
        return Impact(field_name, str(getattr(settings, field_name, "") or ""), str(value),
                      summary=f"Would fetch from {len(chosen)} source(s): {', '.join(chosen)}.",
                      detail=f"Disabled: {', '.join(sorted(set(ALL_SOURCES) - set(chosen))) or 'none'}")
    return _llm_impact(field_name, value, settings, store)


# --- snapshot / rollback --------------------------------------------------------------

@dataclass
class Snapshotter:
    """Copies the encrypted config aside before a write, so any change is one step from
    undone. Cheap insurance: the file is small and a bad config is otherwise the kind of
    thing you fix by remembering what it used to say."""

    path: Path = field(default_factory=lambda: Path("data/secrets.enc"))
    keep: int = 10

    @property
    def dir(self) -> Path:
        return self.path.parent / "config_snapshots"

    def take(self, label: str = "") -> Path | None:
        if not self.path.exists():
            return None
        self.dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        target = self.dir / f"{stamp}{'-' + label if label else ''}.enc"
        shutil.copy2(self.path, target)
        self._prune()
        return target

    def _prune(self) -> None:
        snaps = sorted(self.dir.glob("*.enc"))
        for old in snaps[:-self.keep]:
            old.unlink(missing_ok=True)

    def list(self) -> list[str]:
        return [p.name for p in sorted(self.dir.glob("*.enc"), reverse=True)]

    def restore(self, name: str) -> None:
        source = self.dir / name
        if not source.exists() or source.parent != self.dir:
            raise ConfigRefused(f"no such snapshot: {name}")
        self.take(label="pre-rollback")     # rolling back is itself undoable
        shutil.copy2(source, self.path)
