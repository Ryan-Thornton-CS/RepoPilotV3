"""Offline layout migration with SQLite backups, a journal and rollback on failure."""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3

from repopilotv3.core import PilotError, atomic_json, load_json, now, uid


class StateMigration:
    """Upgrade private paths only after the developer stops other backend instances."""

    def __init__(self, ws):
        """Keep recovery metadata in a fixed location independent of the live layout."""
        self.ws = ws
        self.journal_path = ws.state / "recovery" / "layout" / "current.json"

    def status(self) -> dict:
        """Report upgrade availability and interrupted work without changing state."""
        journal = load_json(self.journal_path, {})
        return {"layout": "legacy" if self.ws.paths.legacy else "organized",
                "migration_required": self.ws.paths.legacy, "journal": journal,
                "recovery_required": journal.get("status") in {"copying", "committing"}}

    @contextlib.contextmanager
    def lease(self, path):
        """Hold an existing legacy lease nonblockingly; never unlink a held lease."""
        with path.open("a+b") as handle:
            try:
                if os.name == "nt":
                    import msvcrt
                    if not path.stat().st_size:
                        handle.write(b"0")
                        handle.flush()
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                raise PilotError("State is busy. Stop every RepoPilot panel, MCP server and hook before organizing it.", "busy") from exc
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)

    def recover(self) -> dict:
        """Restore pre-migration files after an interrupted upgrade, while offline."""
        journal = load_json(self.journal_path, {})
        if journal.get("status") not in {"copying", "committing"}:
            return self.status()
        backup = self.ws.state / "recovery" / "layout" / journal["id"]
        from repopilotv3.core import atomic_bytes
        atomic_bytes(self.ws.config_path, (backup / "config.json").read_bytes())
        # Originals remain at their legacy paths until the commit is journaled.
        for name in ("databases", "runtime"):
            shutil.rmtree(self.ws.state / name, ignore_errors=True)
        journal.update(status="rolled_back", recovered_at=now())
        atomic_json(self.journal_path, journal)
        return self.status()

    def migrate(self, confirmed_stopped: bool = False) -> dict:
        """Copy quiescent databases, verify them, and atomically publish the new layout."""
        if not confirmed_stopped:
            raise PilotError("Confirm all other backend/MCP/hook processes are stopped first.", "confirmation_required")
        if self.status()["recovery_required"]:
            with contextlib.ExitStack() as stack:
                for path in sorted({self.ws.state / "writer.lock", *self.ws.state.glob("operation-*.lock"), *self.ws.state.glob("locks/*.lock")}):
                    stack.enter_context(self.lease(path))
                # Restore configuration before releasing any of its old lease inodes.
                return self.recover()
        config = self.ws.config
        if not self.ws.paths.legacy:
            return self.status()
        for group in ("databases", "locks", "runtime", "recovery"):
            directory = self.ws.state / group
            if directory.is_symlink():
                raise PilotError("Migration refuses linked private-state folders", "unsafe_path")
            if group in {"databases", "runtime"} and directory.exists() and any(directory.iterdir()):
                raise PilotError("Destination already contains files. Inspect state recovery before migrating: " + group, "migration_conflict")
        with contextlib.ExitStack() as stack:
            for path in sorted({self.ws.state / "writer.lock", *self.ws.state.glob("operation-*.lock")}):
                stack.enter_context(self.lease(path))
            migration_id = uid("migration")
            backup = self.ws.state / "recovery" / "layout" / migration_id
            backup.mkdir(parents=True)
            shutil.copy2(self.ws.config_path, backup / "config.json")
            journal = {"id": migration_id, "status": "copying", "at": now(), "files": []}
            atomic_json(self.journal_path, journal)
            try:
                for group in ("databases", "locks", "runtime"):
                    (self.ws.state / group).mkdir(exist_ok=True)
                for source in self.ws.state.glob("*.sqlite3"):
                    destination = self.ws.state / "databases" / source.name
                    with contextlib.closing(sqlite3.connect(source)) as original, contextlib.closing(sqlite3.connect(destination)) as target:
                        original.backup(target)
                        if target.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                            raise PilotError("Database verification failed: " + source.name, "migration_failed")
                    shutil.copy2(destination, backup / source.name)
                    journal["files"].append(source.name)
                for source in self.ws.state.glob("*.lock"):
                    shutil.copy2(source, self.ws.state / "locks" / source.name)
                for name in ("hook-health.json", "last-stop.json"):
                    source = self.ws.state / name
                    if source.exists():
                        shutil.copy2(source, self.ws.state / "runtime" / name)
                journal["status"] = "committing"
                atomic_json(self.journal_path, journal)
                config["schema_version"] = "2.0"
                atomic_json(self.ws.config_path, config)
                journal.update(status="complete", completed_at=now())
                atomic_json(self.journal_path, journal)
            except BaseException:
                self.recover()
                raise
        # Delete old paths only after leases close and the complete journal is durable.
        for name in journal["files"]:
            for suffix in ("", "-wal", "-shm"):
                (self.ws.state / (name + suffix)).unlink(missing_ok=True)
        for pattern in ("*.lock", "hook-health.json", "last-stop.json"):
            for path in self.ws.state.glob(pattern):
                path.unlink(missing_ok=True)
        return self.status()
