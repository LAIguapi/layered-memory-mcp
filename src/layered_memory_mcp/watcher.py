"""
Knowledge Directory Watcher.

Monitors the knowledge directory for file changes and invalidates
caches. Active only in HTTP (long-lived) mode.

v0.5.0: Automatically triggers L0 index sync when files change
        (if auto_sync_l0 is enabled in config).
"""

import logging
import threading
from pathlib import Path

logger = logging.getLogger("layered_memory_mcp.watcher")


class KnowledgeWatcher:
    """Simple polling-based file watcher for the knowledge directory.

    Uses mtime comparison rather than inotify/watchdog to stay zero-dependency.
    For most MCP server deployments, polling every 5s is perfectly adequate.

    v0.5.0: Optionally auto-syncs L0 index on file changes.
    """

    def __init__(
        self,
        knowledge_dir: str | list[str],
        on_change=None,
        poll_interval: float = 5.0,
        # v0.5.0: config for L0 auto-sync
        config=None,
    ):
        """
        Args:
            knowledge_dir: Path or list of paths to watch.
            on_change: Callback invoked with (event_type, filename) on changes.
                       event_type is "created", "modified", or "deleted".
            poll_interval: Seconds between polls (default 5.0).
            config: Optional MemoryConfig for L0 auto-sync on file changes.
                    When provided and auto_sync_l0 is enabled, file changes
                    will trigger a debounced L0 index regeneration.
        """
        if isinstance(knowledge_dir, list):
            self.knowledge_dirs = [Path(d) for d in knowledge_dir]
        else:
            self.knowledge_dirs = [Path(knowledge_dir)]
        self.on_change = on_change
        self.poll_interval = poll_interval
        self.config = config  # v0.5.0

        # v0.5.0: debounce tracking
        self._pending_sync: bool = False
        self._debounce_interval: float = 10.0  # wait 10s after last change before syncing
        self._last_change_time: float = 0.0

        self._snapshot: dict[str, float] = {}  # {relative_path: mtime}
        self._timer: threading.Timer | None = None
        self._sync_timer: threading.Timer | None = None  # v0.5.0
        self._running = False

    def start(self):
        """Start watching."""
        if self._running:
            return
        self._running = True
        self._snapshot = self._take_snapshot()

        # Auto-sync on startup if config is available
        if self._should_auto_sync():
            self._sync_l0()

        logger.info("Knowledge watcher started: %s (auto_sync_l0=%s)",
                     [str(d) for d in self.knowledge_dirs],
                     self.config.auto_sync_l0 if self.config else False)
        self._schedule_poll()

    def stop(self):
        """Stop watching."""
        self._running = False
        if self._timer:
            self._timer.cancel()
            self._timer = None
        if self._sync_timer:
            self._sync_timer.cancel()
            self._sync_timer = None
        logger.info("Knowledge watcher stopped")

    def _should_auto_sync(self) -> bool:
        """Check if L0 auto-sync is enabled."""
        return (
            self.config is not None
            and self.config.auto_sync_l0
            and self.config.l0_index_file is not None
        )

    def _sync_l0(self):
        """Trigger L0 index sync (non-blocking)."""
        if not self._should_auto_sync():
            return
        try:
            from .l0_manager import sync_l0_index
            report = sync_l0_index(self.config, dry_run=False)
            if report.get("entries_added", 0) or report.get("entries_removed", 0):
                logger.info("Watcher auto-sync L0: +%d -%d entries, %d total",
                            report.get("entries_added", 0),
                            report.get("entries_removed", 0),
                            report.get("total_entries", 0))
        except Exception as e:
            logger.warning("Watcher L0 sync failed: %s", e)

    def _schedule_sync(self):
        """Schedule a debounced L0 sync."""
        self._pending_sync = True
        if self._sync_timer:
            self._sync_timer.cancel()
        self._sync_timer = threading.Timer(self._debounce_interval, self._perform_sync)
        self._sync_timer.daemon = True
        self._sync_timer.start()

    def _perform_sync(self):
        """Execute the debounced sync."""
        self._pending_sync = False
        self._sync_l0()

    def _schedule_poll(self):
        if not self._running:
            return
        self._timer = threading.Timer(self.poll_interval, self._poll)
        self._timer.daemon = True
        self._timer.start()

    def _poll(self):
        if not self._running:
            return
        try:
            new_snapshot = self._take_snapshot()
            changes = self._detect_changes(new_snapshot)
            self._snapshot = new_snapshot

            # v0.5.0: if files changed and auto-sync is on, schedule L0 sync
            if changes and self._should_auto_sync():
                import time
                self._last_change_time = time.time()
                self._schedule_sync()
        except Exception as e:
            logger.warning("Watcher poll error: %s", e)
        finally:
            self._schedule_poll()

    def _take_snapshot(self) -> dict[str, float]:
        """Take a snapshot of current file mtimes across all watched directories.

        Uses '{dir_index}/{rel}' as key to avoid collisions when multiple
        directories contain files with the same relative path.
        """
        snapshot: dict[str, float] = {}
        for idx, kdir in enumerate(self.knowledge_dirs):
            if not kdir.exists():
                continue
            for f in kdir.rglob("*.md"):
                try:
                    rel = str(f.relative_to(kdir))
                    snapshot[f"{idx}/{rel}"] = f.stat().st_mtime
                except OSError:
                    continue
        return snapshot

    def _detect_changes(self, new_snapshot: dict[str, float]) -> bool:
        """Compare snapshots and fire callbacks. Returns True if any changes detected."""
        old = self._snapshot
        changed = False

        # New files
        for name in new_snapshot:
            if name not in old:
                self._fire("created", name)
                changed = True

        # Deleted files
        for name in old:
            if name not in new_snapshot:
                self._fire("deleted", name)
                changed = True

        # Modified files
        for name in new_snapshot:
            if name in old and new_snapshot[name] != old[name]:
                self._fire("modified", name)
                changed = True

        return changed

    def _fire(self, event_type: str, filename: str):
        logger.debug("Knowledge %s: %s", event_type, filename)
        if self.on_change:
            try:
                self.on_change(event_type, filename)
            except Exception as e:
                logger.warning("Watcher callback error: %s", e)
