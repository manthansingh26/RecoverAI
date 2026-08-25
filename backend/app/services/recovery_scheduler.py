"""Recovery Scheduler — periodic automatic execution of due PENDING_EXECUTION cases.

This module is a TIMER ONLY. It contains no business logic, eligibility checks,
retry policy, strategy selection, or human approval logic. All of that lives in
the existing recovery executor (recovery_executor.py).

Responsibilities:
- Periodically call execute_due_cases() using a fresh DB session each cycle.
- Offload synchronous SQLAlchemy work to a thread pool via asyncio.to_thread().
- Isolate exceptions so a failed cycle never crashes FastAPI.
- Support graceful shutdown that allows an in-flight cycle to finish.

Design:
- Enabled/disabled via SCHEDULER_ENABLED config.
- Interval controlled via SCHEDULER_INTERVAL_SECONDS config.
- Uses asyncio.Event for cooperative shutdown signalling.
- Each cycle gets a fresh SessionLocal() session, closed in a finally block.
- SELECT FOR UPDATE SKIP LOCKED in execute_due_cases() handles all concurrency
  safety across reload overlaps and multi-worker scenarios.

Shutdown design (critical — read before changing):
- stop() sets shutdown_event then simply awaits the task.
- Setting shutdown_event immediately unblocks the interval-wait (idle case).
- For a mid-cycle shutdown the current cycle runs to completion first;
  a post-cycle check detects the event and exits without starting the wait.
- task.cancel() is intentionally NOT called in stop(): it would inject
  CancelledError into asyncio.to_thread(), abandoning the asyncio-side
  future while the worker thread continues executing the DB operation
  without anyone awaiting the result — an orphaned thread.
- asyncio.shield is intentionally NOT used on shutdown_event.wait():
  shield creates an internal Task on each loop iteration that becomes
  orphaned when wait_for times out, producing RuntimeWarnings at
  event-loop teardown.

Usage (called from FastAPI lifespan in main.py):
    scheduler = RecoveryScheduler(interval_seconds=settings.SCHEDULER_INTERVAL_SECONDS)
    await scheduler.start()
    ...
    await scheduler.stop()
"""

import asyncio
import copy
import logging
import threading

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.core.metrics import metrics
from app.db.session import SessionLocal
from app.services.recovery_executor import ExecutionSummary, execute_due_cases

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scheduler status / heartbeat (Milestone 15B)
# ---------------------------------------------------------------------------

@dataclass
class SchedulerStatus:
    """Read-only snapshot of scheduler liveness and last-cycle statistics.

    Process-local only (in-memory). Not persisted across restarts by design —
    the scheduler is an in-process component; durability is not required to
    answer "is the scheduler alive and is it making progress".
    """

    running: bool = False
    last_cycle_started_at: datetime | None = None
    last_cycle_finished_at: datetime | None = None
    last_cycle_duration_ms: int | None = None
    last_attempted: int = 0
    last_succeeded: int = 0
    last_failed: int = 0
    last_blocked: int = 0
    last_error: str | None = None
    total_cycles: int = 0


class _SchedulerStatusStore:
    """Thread-safe store updated from the scheduler worker thread."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._status = SchedulerStatus()

    def snapshot(self) -> SchedulerStatus:
        with self._lock:
            return copy.deepcopy(self._status)

    def mark_running(self, running: bool) -> None:
        with self._lock:
            self._status.running = running

    def record_cycle(
        self,
        *,
        started: datetime,
        finished: datetime,
        duration_ms: int,
        attempted: int,
        succeeded: int,
        failed: int,
        blocked: int,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._status.last_cycle_started_at = started
            self._status.last_cycle_finished_at = finished
            self._status.last_cycle_duration_ms = duration_ms
            self._status.last_attempted = attempted
            self._status.last_succeeded = succeeded
            self._status.last_failed = failed
            self._status.last_blocked = blocked
            self._status.last_error = error
            self._status.total_cycles += 1


# Shared module-level status store (single-process deployment).
_scheduler_status = _SchedulerStatusStore()


def get_scheduler_status() -> SchedulerStatus:
    """Return a thread-safe snapshot of the current scheduler status."""
    return _scheduler_status.snapshot()


# ---------------------------------------------------------------------------
# Synchronous cycle — runs inside asyncio.to_thread()
# ---------------------------------------------------------------------------

def run_one_cycle(db: Session | None = None) -> ExecutionSummary:
    """Execute one scheduler cycle: find and process all due PENDING_EXECUTION cases.

    This function is SYNCHRONOUS because execute_due_cases() uses synchronous
    SQLAlchemy. It is called via asyncio.to_thread() to avoid blocking the
    FastAPI event loop.

    If a ``db`` session is provided it is used as-is (useful for testing).
    If ``db`` is None a fresh SessionLocal() is created and closed inside
    this function's own finally block.

    Exceptions PROPAGATE to the caller (scheduler_loop catches them) — this
    function does not swallow failures, it records them in SchedulerStatus.

    Args:
        db: Optional pre-existing DB session. When None a fresh session is
            created and owned by this function.

    Returns:
        ExecutionSummary with attempted/succeeded/failed/blocked counts.
    """
    own_session = db is None
    if own_session:
        db = SessionLocal()

    started = datetime.now(timezone.utc)
    metrics.increment("scheduler_cycles_total")
    try:
        logger.debug("Scheduler cycle started")
        summary = execute_due_cases(db)

        finished = datetime.now(timezone.utc)
        duration_ms = int((finished - started).total_seconds() * 1000)
        _scheduler_status.record_cycle(
            started=started,
            finished=finished,
            duration_ms=duration_ms,
            attempted=summary.attempted,
            succeeded=summary.succeeded,
            failed=summary.failed,
            blocked=summary.blocked,
        )

        if summary.attempted > 0:
            logger.info(
                "Scheduler cycle completed: attempted=%d succeeded=%d "
                "failed=%d blocked=%d duration_ms=%d",
                summary.attempted,
                summary.succeeded,
                summary.failed,
                summary.blocked,
                duration_ms,
            )
        else:
            logger.debug("Scheduler cycle completed: no due cases found")

        return summary
    except Exception as e:
        metrics.increment("scheduler_failed_cycles_total")
        finished = datetime.now(timezone.utc)
        duration_ms = int((finished - started).total_seconds() * 1000)
        _scheduler_status.record_cycle(
            started=started,
            finished=finished,
            duration_ms=duration_ms,
            attempted=0,
            succeeded=0,
            failed=0,
            blocked=0,
            error=str(e),
        )
        raise
    finally:
        if own_session:
            db.close()


# ---------------------------------------------------------------------------
# Async scheduler loop
# ---------------------------------------------------------------------------

async def scheduler_loop(
    shutdown_event: asyncio.Event,
    interval_seconds: int,
) -> None:
    """Async scheduler loop that runs until shutdown_event is set.

    Each iteration:
    1. Runs run_one_cycle() via asyncio.to_thread() (non-blocking).
    2. After the cycle completes, checks shutdown_event before waiting.
    3. Waits for interval_seconds OR shutdown, whichever comes first.

    Shutdown correctness:
    - shutdown_event.set() immediately wakes the interval-wait (idle case).
    - For a mid-cycle shutdown: the cycle runs to completion, then the
      post-cycle check exits the loop without starting the interval-wait.
    - asyncio.CancelledError is NOT caught here (only generic Exception).
      If external code cancels the task, CancelledError propagates and
      the loop exits — that is the correct external-cancellation behaviour.
    - asyncio.shield is intentionally absent: it would create an orphaned
      Event.wait() Task on every timeout, causing RuntimeWarnings at
      event-loop teardown.

    Args:
        shutdown_event: Set this event to signal the loop to stop.
        interval_seconds: Seconds to wait between cycles. Must be >= 1.
    """
    logger.info("Scheduler loop started (interval=%ds)", interval_seconds)

    while not shutdown_event.is_set():
        # --- Run cycle -------------------------------------------------------
        # asyncio.to_thread offloads the synchronous SQLAlchemy work so the
        # event loop remains responsive during execution.
        # Only generic Exception is caught — CancelledError propagates so that
        # external task cancellation still exits the loop immediately.
        try:
            await asyncio.to_thread(run_one_cycle)
        except asyncio.CancelledError:
            logger.info("Scheduler loop cancelled during cycle")
            raise
        except Exception:
            logger.exception("Scheduler cycle failed — will retry after interval")

        # --- Post-cycle shutdown check ----------------------------------------
        # If shutdown was requested while the cycle was running, exit now
        # without entering the interval-wait. This is the core of the
        # graceful-shutdown contract: the cycle completes fully, then we exit.
        if shutdown_event.is_set():
            break

        # --- Interval wait ---------------------------------------------------
        # Wait for interval_seconds OR an immediate wakeup from shutdown_event,
        # whichever comes first.
        #
        # Plain asyncio.wait_for (no asyncio.shield) is correct:
        # - When shutdown_event is set, Event.wait() returns immediately and
        #   wait_for returns without raising — we break.
        # - When the timeout elapses, wait_for cancels the inner wait() cleanly
        #   (Event.wait() handles CancelledError by re-raising; wait_for then
        #   converts it to TimeoutError). No orphaned tasks, no warnings.
        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=float(interval_seconds),
            )
            break  # shutdown_event was set — exit the loop
        except asyncio.TimeoutError:
            pass  # interval elapsed normally — run the next cycle

    logger.info("Scheduler loop exiting")


# ---------------------------------------------------------------------------
# Scheduler lifecycle manager
# ---------------------------------------------------------------------------

class RecoveryScheduler:
    """Manages the lifecycle of the background scheduler task.

    Provides clean start/stop semantics compatible with FastAPI's lifespan
    context manager. A single asyncio.Task is created on start and awaited
    (never cancelled) on stop.

    Shutdown sequence:
    1. Set shutdown_event.
       - If idle (between cycles): immediately wakes the interval-wait;
         the loop breaks and the task completes promptly.
       - If mid-cycle: the cycle runs to completion, then the post-cycle
         check detects the event and exits.
    2. Await the task (no task.cancel()).
       - task.cancel() is intentionally absent: cancelling the task while
         it is in asyncio.to_thread() would abandon the asyncio-side
         future while the worker thread continues running the DB operation
         without anyone awaiting its result.
    3. Task completes cooperatively; event loop is clean for lifespan teardown.
    """

    def __init__(self, interval_seconds: int) -> None:
        self._interval = interval_seconds
        self._shutdown_event: asyncio.Event | None = None
        self._task: asyncio.Task | None = None  # type: ignore[type-arg]

    async def start(self) -> None:
        """Start the background scheduler loop."""
        self._shutdown_event = asyncio.Event()
        self._task = asyncio.create_task(
            scheduler_loop(self._shutdown_event, self._interval),
            name="recovery_scheduler",
        )
        _scheduler_status.mark_running(True)
        logger.info(
            "Recovery scheduler started (interval=%ds)", self._interval
        )

    async def stop(self) -> None:
        """Stop the scheduler gracefully.

        Sets the shutdown event and awaits task completion.

        If idle: returns promptly (shutdown_event wakes the interval-wait).
        If mid-cycle: waits for the current cycle to finish naturally, then
        returns.

        task.cancel() is intentionally NOT called — see class docstring.
        """
        if self._task is None:
            return

        logger.info("Recovery scheduler stopping…")

        # Signal shutdown.
        # - Wakes the interval-wait immediately if the loop is idle.
        # - Detected after cycle completion if mid-cycle.
        if self._shutdown_event is not None:
            self._shutdown_event.set()

        # Await task completion.
        # No task.cancel() — the loop exits cooperatively via shutdown_event.
        try:
            await self._task
        except asyncio.CancelledError:
            # Absorb if external code cancelled our task.
            pass
        except Exception:
            logger.exception("Scheduler task raised an unexpected exception on stop")

        self._task = None
        self._shutdown_event = None
        _scheduler_status.mark_running(False)
        logger.info("Recovery scheduler stopped")

    @property
    def is_running(self) -> bool:
        """True if the scheduler task is currently active."""
        return self._task is not None and not self._task.done()
