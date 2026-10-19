"""Independent lightweight monitoring with fingerprints and warnings emitted once."""

from __future__ import annotations

import contextlib
import json
import sqlite3

from repopilotv3.core import canonical, digest, now, uid
from repopilotv3.manual.sessions import measure


class Monitor:
    """Record bounded metadata in a database independent of the repository writer."""

    def __init__(self, ws):
        """Choose an independent local SQLite store; do not touch the source index."""
        self.path = ws.state / "monitor.sqlite3"

    @contextlib.contextmanager
    def db(self):
        """Keep each monitoring transaction short with a bounded contention timeout."""
        db = sqlite3.connect(self.path, timeout=0.2)
        try:
            db.executescript("""CREATE TABLE IF NOT EXISTS events(
                id TEXT PRIMARY KEY, run TEXT, tool TEXT, fingerprint TEXT, success INTEGER,
                value TEXT, at TEXT);
                CREATE TABLE IF NOT EXISTS notices(run TEXT, condition TEXT, PRIMARY KEY(run,condition));
                CREATE TABLE IF NOT EXISTS bindings(session TEXT PRIMARY KEY, run TEXT);
            """)
            yield db
            db.commit()
        finally:
            db.close()

    def bind(self, session: str, task: str) -> None:
        """Bind observed checkpoint results, never the global active task."""
        with self.db() as db:
            db.execute("INSERT OR REPLACE INTO bindings VALUES(?,?)", (session, task))

    def bound(self, session: str) -> str | None:
        """Return only a binding positively observed for this external session."""
        with self.db() as db:
            row = db.execute("SELECT run FROM bindings WHERE session=?", (session,)).fetchone()
        return row[0] if row else None

    def record(
        self,
        run: str,
        tool: str,
        arguments: dict,
        result,
        snapshot: str = "",
        success: bool = True,
        event_id: str | None = None,
        coverage: str = "managed",
    ) -> dict:
        """Warn once for repetition, failed retries, and calls since a substantive checkpoint."""
        event_id = event_id or uid("event")
        # Drop wrapper metadata without erasing substantive source text or stable task IDs.
        normalized = stable(result)
        if tool == "validate":
            evidence = result.get("validation", {}) if isinstance(result, dict) else {}
            normalized = {
                k: evidence.get(k) for k in ["status", "profile", "profile_hash", "test_counters", "error"]
            }
        elif tool == "run_command" and isinstance(result, dict):
            normalized = {k: result.get(k) for k in ["status", "exit_code", "output", "error"]}
        fingerprint = digest(canonical([tool, arguments, snapshot, bool(success), normalized]))
        content = measure(canonical(result))
        value = {**content, "coverage": coverage}
        warnings = []
        with self.db() as db:
            inserted = db.execute(
                "INSERT OR IGNORE INTO events VALUES(?,?,?,?,?,?,?)",
                (event_id, run, tool, fingerprint, int(success), canonical(value), now()),
            ).rowcount
            if inserted:
                repeated = db.execute(
                    "SELECT count(*) FROM events WHERE run=? AND fingerprint=?", (run, fingerprint)
                ).fetchone()[0]
                last_checkpoint = (
                    db.execute(
                        "SELECT max(rowid) FROM events WHERE run=? AND tool='checkpoint'", (run,)
                    ).fetchone()[0]
                    or 0
                )
                calls = db.execute(
                    "SELECT count(*) FROM events WHERE run=? AND rowid>? AND tool!='checkpoint'",
                    (run, last_checkpoint),
                ).fetchone()[0]
                conditions = []
                if success and repeated >= 3 and tool != "checkpoint":
                    conditions.append(
                        (
                            "repeat:" + fingerprint,
                            "Repeated unchanged result; reuse it or narrow the request.",
                        )
                    )
                if not success and repeated >= 3:
                    conditions.append(
                        (
                            "failure:" + fingerprint,
                            "Repeated failures with the same tool and arguments; inspect diagnostics and current source before retrying. "
                            + ("Source changes are not observable in this hook event." if coverage == "partial" else "Change the source or approach before retrying."),
                        )
                    )
                if calls >= 20:
                    conditions.append(
                        (f"checkpoint:{last_checkpoint}", "Save a TODO checkpoint and choose the next step.")
                    )
                if run == "unattributed":
                    conditions = []
                for key, message in conditions:
                    if db.execute("INSERT OR IGNORE INTO notices VALUES(?,?)", (run, key)).rowcount:
                        warnings.append(message)
        return {"event_id": event_id, "run_id": run, "warnings": warnings, **value}

    def checkpoint(self, task: dict, run_id: str) -> dict:
        """Deduplicate identical progress so replayed checkpoints cannot reset counters."""
        substantive = {
            k: task[k]
            for k in [
                "id",
                "objective",
                "items",
                "decisions",
                "errors",
                "questions",
                "assumptions",
                "files",
                "required_tests",
            ]
        }
        return self.record(
            run_id,
            "checkpoint",
            {},
            substantive,
            event_id="checkpoint-" + digest(run_id + canonical(substantive)),
        )

    def status(self) -> dict:
        """Return concise coverage and local estimates without claiming billing savings."""
        with self.db() as db:
            rows = db.execute("SELECT value FROM events").fetchall()
            bindings = db.execute("SELECT count(*) FROM bindings").fetchone()[0]
        values = [json.loads(r[0]) for r in rows]
        return {
            "calls": len(values),
            "estimated_returned_tokens": sum(v["estimated_tokens"] for v in values),
            "bindings": bindings,
            "coverage": "partial: only observed native events and managed results",
            "mode": "warn",
        }


def stable(value):
    """Exclude volatile transport/timing metadata from repetition fingerprints."""
    if isinstance(value, dict):
        return {
            k: stable(v)
            for k, v in value.items()
            if k
            not in {
                "at",
                "created_at",
                "updated_at",
                "duration_seconds",
                "request_id",
                "monitoring",
                "event_id",
            }
        }
    if isinstance(value, list):
        return [stable(v) for v in value]
    return value
