"""Ingestion runner — drives adapters into the store.

For each enabled adapter: fetch postings, upsert (dedup by hash), count new vs.
re-seen, and log one `ingest` event per adapter run. Resilient: a failing adapter
logs an `error` event and the run continues with the rest.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jobagent.core.schemas import Event
from jobagent.ingestion.base import BaseAdapter
from jobagent.store import Store


@dataclass
class AdapterResult:
    source: str
    fetched: int = 0
    new: int = 0
    error: str | None = None


@dataclass
class RunReport:
    results: list[AdapterResult] = field(default_factory=list)

    @property
    def total_new(self) -> int:
        return sum(r.new for r in self.results)

    @property
    def total_fetched(self) -> int:
        return sum(r.fetched for r in self.results)


def run_ingestion(adapters: list[BaseAdapter], store: Store, *, run_id: str | None = None) -> RunReport:
    """Drive every enabled adapter once.

    `run_id` is the observability spine: the same id rides every event this pass
    emits (here, matching, and the pipeline summary), so one slow or failing run can
    be reconstructed from the events table instead of guessed at from timestamps.
    """
    report = RunReport()
    for adapter in adapters:
        src = adapter.source.value
        if not adapter.enabled:
            continue
        result = AdapterResult(source=src)
        try:
            for job in adapter.fetch():
                is_new = store.is_new_job(job)
                store.upsert_job(job)
                result.fetched += 1
                if is_new:
                    result.new += 1
            store.log_event(Event(kind="ingest", payload={
                "source": src, "fetched": result.fetched, "new": result.new, "run_id": run_id,
            }))
        except Exception as exc:  # noqa: BLE001 — one bad source must not kill the run
            result.error = f"{type(exc).__name__}: {exc}"
            store.log_event(Event(kind="error", payload={
                "source": src, "error": result.error, "run_id": run_id,
            }))
        report.results.append(result)
    return report
