"""Debounced repository notifications with polling fallback for automatic context refresh."""

from __future__ import annotations

import threading
from pathlib import Path

from repopilotv3.repository.index import SKIP_DIRS


class RepositoryWatcher:
    """Mark potentially relevant changes cheaply; indexing runs outside watcher callbacks."""

    def __init__(self, root: Path):
        """Create a change signal without starting background threads."""
        self.root = root
        self.dirty = threading.Event()
        self.observer = None
        self.mode = "polling"

    def start(self) -> None:
        """Use watchdog when available; periodic refresh remains the correctness fallback."""
        try:
            from watchdog.events import FileSystemEventHandler
            from watchdog.observers import Observer

            owner = self

            class Handler(FileSystemEventHandler):
                """Notify on source, configuration, and Git changes without doing analysis."""

                def on_any_event(self, event):
                    """Ignore private/generated output to avoid export-triggered loops."""
                    if event.event_type in {"opened", "closed_no_write"}:
                        return
                    try:
                        parts = Path(event.src_path).relative_to(owner.root).parts
                    except ValueError:
                        return
                    if not any(p in SKIP_DIRS - {".git"} for p in parts):
                        owner.dirty.set()

            self.observer = Observer()
            self.observer.schedule(Handler(), str(self.root), recursive=True)
            self.observer.start()
            self.mode = "watchdog + polling"
        except (ImportError, OSError, RuntimeError):
            self.mode = "polling"
            self.stop()
            self.observer = None

    def stop(self) -> None:
        """Release background watcher resources on exit."""
        if self.observer:
            try:
                self.observer.stop()
                if self.observer.is_alive():
                    self.observer.join(timeout=2)
            except (OSError, RuntimeError):
                pass
