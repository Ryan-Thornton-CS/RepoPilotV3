"""Durable append-only chat entries and UI preferences, separate from source indexing."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager

from repopilotv3.core import canonical, now, uid
from repopilotv3.workflow.turns import ConversationTurns


class HistoryStore:
    """Persist display content without coupling transcript reads to repository writer locks."""

    def __init__(self, ws):
        """Create the small history store under this host's private workspace state."""
        self.ws = ws
        ws.config
        self.path = ws.paths.database("history.sqlite3")
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS entries(
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT NOT NULL,
                    id TEXT NOT NULL, kind TEXT NOT NULL, at TEXT NOT NULL, body TEXT NOT NULL,
                    UNIQUE(session,id));
                CREATE INDEX IF NOT EXISTS entry_session ON entries(session,sequence);
                CREATE TABLE IF NOT EXISTS preferences(id TEXT PRIMARY KEY, value TEXT NOT NULL);
            """)

    @contextmanager
    def db(self, timeout: float = 5):
        """Keep history transactions short and release connections even after a failure."""
        db = sqlite3.connect(self.path, timeout=timeout)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        finally:
            db.close()

    def add(self, session: str, kind: str, body, entry_id: str | None = None) -> str:
        """Append one idempotent display event, retaining full local content privately."""
        entry_id = entry_id or uid("entry")
        from repopilotv3.manual.sessions import deletion_path

        if deletion_path(self.ws, session).exists():
            return entry_id
        with self.db() as db:
            db.execute(
                "INSERT OR IGNORE INTO entries(session,id,kind,at,body) VALUES(?,?,?,?,?)",
                (session, entry_id, kind, now(), canonical(body)),
            )
        return entry_id

    def purge(self, session: str) -> None:
        """Remove chat-only presentation data without acquiring the repository writer."""
        with self.db(timeout=0.1) as db:
            db.execute("DELETE FROM entries WHERE session=?", (session,))
            for prefix in ("draft:", "starter:", "full_source:", "package-dirty:"):
                db.execute("DELETE FROM preferences WHERE id=?", (prefix + session,))
            for prefix in ("reply-output:", "context-advised:"):
                key = prefix + session + ":"
                db.execute("DELETE FROM preferences WHERE substr(id,1,?)=?", (len(key), key))
            db.execute("DELETE FROM preferences WHERE id='selected_session' AND value=?", (canonical(session),))

    def page(self, session: str, before: int | None = None, limit: int = 60) -> list[dict]:
        """Return a bounded chronological page; reading history never dispatches tools."""
        with self.db() as db:
            rows = db.execute(
                "SELECT * FROM entries WHERE session=? AND sequence<? ORDER BY sequence DESC LIMIT ?",
                (session, before or 2**63 - 1, min(max(limit, 1), 100)),
            ).fetchall()
        page, size = [], 0
        for row in rows:
            entry_size = len(row["body"].encode("utf-8"))
            if page and size + entry_size > 8 * 1024 * 1024:
                break
            size += entry_size
            page.append(ConversationTurns.decode(row))
        return list(reversed(page))

    def preference(self, key: str, value=None):
        """Read or atomically replace UI state; an empty draft is a valid stored value."""
        with self.db() as db:
            if value is not None:
                db.execute(
                    "INSERT INTO preferences VALUES(?,?) ON CONFLICT(id) DO UPDATE SET value=excluded.value",
                    (key, canonical(value)),
                )
                return value
            row = db.execute("SELECT value FROM preferences WHERE id=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None
