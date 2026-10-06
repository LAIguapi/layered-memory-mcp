"""Periodic framework self-maintenance (v3.4.0).

Until now the framework's self-maintenance ran only as a *ride-along*: every
``inject_knowledge`` call — and ``get_l0_index``, as a backstop — carried a
best-effort ``auto_maintain_after_write``. That works while somebody is calling
the server, which is the wrong dependence for a daemon: a long-idle service
never compacts, never notices agent memory creeping up on its limit, and never
heals a deployed guard that drifted a version behind the package.

This module closes that gap where the framework's own design notes said it
belongs — inside the framework, not delegated to an external cron job. On an
HTTP (long-lived) server a daemon thread ticks on an interval and calls the same
entry points the ride-along uses. Those entry points are themselves interval- and
threshold-gated, so a tick is cheap and the loop only does work when the
framework's own policy says it is due.

Deliberately out of scope:

* **stdio servers.** They are per-session and short-lived, and stdout carries the
  JSON-RPC stream — a background thread must never write there. The ride-along
  already covers them.
* **A full rot audit.** ``audit_rot`` is an O(n²) scan (seconds of CPU on a real
  store) and the health-watchdog cron already runs it weekly; paying that cost
  every tick to emit a log line nobody reads is not worth it.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable

logger = logging.getLogger(__name__)

DEFAULT_TICK_SECONDS = 1800.0
DEFAULT_INITIAL_DELAY = 60.0

# Guard rail for real deployments: the underlying maintenance is gated on
# intervals measured in days, so anything under half a minute is a
# misconfiguration (usually a seconds/milliseconds mix-up), not a preference.
MIN_TICK_SECONDS = 30.0


def maintenance_task(config: Any) -> dict:
    """One round of framework self-maintenance. Never raises.

    Returns a report dict; individual keys are absent when that job did not run
    or is not applicable, and carry ``{"error": ...}`` when it failed.
    """
    report: dict = {}

    # 1. Agent-memory compaction + L1 line-level dedup. Both are gated inside on
    #    usage ratio / elapsed interval, so most ticks are a cheap read.
    try:
        from .memory_compactor import auto_maintain_after_write

        report["auto_maintain"] = auto_maintain_after_write(config)
    except Exception as e:  # noqa: BLE001 — maintenance must never break the host
        logger.warning("maintenance: auto-maintain failed: %s", e)
        report["auto_maintain"] = {"error": str(e)}

    # 2. Deployed guard refresh. The host's guard is a copy of the payload
    #    bundled in this package, so it is a version behind after every upgrade.
    #    Files-only copy, auto policy only — a manual host keeps the human in
    #    the loop (same rule as the get_l0_index ride-along).
    if str(getattr(config, "write_guard", "manual")).strip().lower() == "auto":
        try:
            from .agent_integrator import detect_agent_type
            from .write_guard import refresh_deployed_plugin

            home = (detect_agent_type() or {}).get("home_dir")
            if home:
                report["write_guard"] = refresh_deployed_plugin(
                    home, "auto", config.home / "write_guard_state.json"
                )
        except Exception as e:  # noqa: BLE001 — a repair must never break the host
            logger.debug("maintenance: guard refresh skipped: %s", e)

    return report


class MaintenanceLoop:
    """A daemon thread that runs ``task`` every ``interval`` seconds.

    ``start`` is idempotent, ``stop`` is prompt (the waits are interruptible, so
    a long interval never delays shutdown), and a task that raises is logged and
    retried on the next tick rather than killing the loop.
    """

    def __init__(
        self,
        task: Callable[[], Any],
        interval: float = DEFAULT_TICK_SECONDS,
        initial_delay: float = DEFAULT_INITIAL_DELAY,
        name: str = "layered-memory-maintenance",
    ) -> None:
        self._task = task
        self._interval = max(0.001, float(interval))
        self._initial_delay = max(0.0, float(initial_delay))
        self._name = name
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        """Start the loop. Returns False when it is already running."""
        if self.running:
            return False
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
        self._thread.start()
        return True

    def stop(self, timeout: float = 5.0) -> bool:
        """Signal the loop to stop and wait for it. True when it is gone."""
        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout)
            return not thread.is_alive()
        return True

    def run_once(self) -> Any:
        """Run the task once, swallowing failures exactly as the loop does.

        The manual/one-shot entry point — also what makes the loop testable
        without waiting on a tick.
        """
        try:
            return self._task()
        except Exception as e:  # noqa: BLE001 — see class docstring
            logger.warning("maintenance tick failed (%s): %s", self._name, e)
            return None

    def _run(self) -> None:
        if self._stop_event.wait(self._initial_delay):
            return
        while not self._stop_event.is_set():
            self.run_once()
            if self._stop_event.wait(self._interval):
                break


def start_maintenance(config: Any, task: Callable[[], Any] | None = None) -> MaintenanceLoop | None:
    """Start the periodic loop when the config asks for it, else return None.

    Never raises: a host whose maintenance cannot start must still serve reads.
    """
    if not getattr(config, "maintenance_enabled", True):
        logger.info("maintenance loop disabled by config")
        return None

    try:
        interval = float(
            getattr(config, "maintenance_tick_seconds", DEFAULT_TICK_SECONDS)
            or DEFAULT_TICK_SECONDS
        )
    except (TypeError, ValueError):
        interval = DEFAULT_TICK_SECONDS
    if interval < MIN_TICK_SECONDS:
        logger.warning(
            "maintenance tick %ss is below the %ss floor; using the floor",
            interval,
            MIN_TICK_SECONDS,
        )
        interval = MIN_TICK_SECONDS

    try:
        delay = max(
            0.0, float(getattr(config, "maintenance_initial_delay", DEFAULT_INITIAL_DELAY) or 0.0)
        )
    except (TypeError, ValueError):
        delay = DEFAULT_INITIAL_DELAY

    loop = MaintenanceLoop(
        task or (lambda: maintenance_task(config)),
        interval=interval,
        initial_delay=delay,
    )
    loop.start()
    logger.info(
        "maintenance loop started: every %ss (initial delay %ss)", interval, delay
    )
    return loop
