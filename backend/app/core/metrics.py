"""Lightweight in-process operational counters (Milestone 15B).

Process-local only. Counters reset on restart and are explicitly documented as
such. This is NOT Prometheus/OpenTelemetry — it is the smallest justified
instrumentation for a single-process competition/deployment system.

Security: only aggregate counts are stored here. NEVER store secrets, session
tokens, raw payloads, customer PII, or anything sensitive.
"""

import threading
from typing import Any


class Metrics:
    """Thread-safe monotonic counters exposed via ``snapshot()``."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {}

    def increment(self, name: str, by: float = 1.0) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + by

    def add(self, name: str, value: float) -> None:
        with self._lock:
            self._counters[name] = self._counters.get(name, 0.0) + value

    def get(self, name: str) -> float:
        with self._lock:
            return self._counters.get(name, 0.0)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._counters)


# Shared module-level metrics instance.
metrics = Metrics()


def grouped_snapshot() -> dict[str, dict[str, float]]:
    """Return counters grouped by subsystem for the /metrics endpoint."""
    raw = metrics.snapshot()

    webhook = {
        "received": raw.get("webhook_received", 0.0),
        "verified": raw.get("webhook_verified", 0.0),
        "rejected_hmac": raw.get("webhook_rejected_hmac", 0.0),
        "rejected_stale": raw.get("webhook_rejected_stale", 0.0),
        "duplicate": raw.get("webhook_duplicate", 0.0),
        "malformed": raw.get("webhook_malformed", 0.0),
        "captured_resolved": raw.get("webhook_captured_resolved", 0.0),
        "captured_stale": raw.get("webhook_captured_stale", 0.0),
        "processing_seconds_total": raw.get("webhook_processing_seconds_total", 0.0),
    }
    scheduler = {
        "cycles_total": raw.get("scheduler_cycles_total", 0.0),
        "failed_cycles_total": raw.get("scheduler_failed_cycles_total", 0.0),
    }
    execution = {
        "attempts_total": raw.get("execution_attempts_total", 0.0),
        "failures_total": raw.get("execution_failures_total", 0.0),
    }
    return {"webhook": webhook, "scheduler": scheduler, "execution": execution}
