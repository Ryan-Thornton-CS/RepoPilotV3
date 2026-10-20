"""Explicit conversation bindings and locally observed transfer accounting."""

from __future__ import annotations

import threading
from contextlib import ExitStack, contextmanager
from functools import wraps

from repopilotv3.core import PilotError, Workspace, canonical, digest, now, uid
from repopilotv3.tasks.store import TaskStore

_ACTIVITIES = threading.local()


def deletion_path(ws: Workspace, session_id: str):
    """Return the private deletion marker path without trusting a caller-supplied filename."""
    return ws.state / "deleted-sessions" / (digest(session_id) + ".json")


def session_operation(method):
    """Guard session mutations against concurrent deletion, including nested service calls."""
    @wraps(method)
    def guarded(self, session_id: str, *args, **kwargs):
        """Hold one session lease while preserving the wrapped service's return contract."""
        with SessionStore(self.ws).activity(session_id):
            return method(self, session_id, *args, **kwargs)
    return guarded


def measure(text: str) -> dict:
    """Estimate tokens conservatively from UTF-8 bytes and Unicode characters."""
    size = len(text.encode("utf-8"))
    return {
        "bytes": size,
        "characters": len(text),
        "estimated_tokens": int(max(len(text) / 3.2, size / 4) + 0.999),
        "method": "max(characters/3.2, UTF-8 bytes/4)",
        "confidence": "low",
    }


class SessionStore:
    """Bind each manual chat independently; never infer a global active task."""

    def __init__(self, ws: Workspace):
        """Attach session and event storage to one repository."""
        self.ws = ws

    def create(self, task_id: str | None = None, project: str = "default") -> dict:
        """Register a local conversation without asserting any external transfer."""
        if task_id:
            TaskStore(self.ws).get(task_id)
        session = {
            "id": uid("chat"),
            "task_id": task_id,
            "project": project,
            "todo_ready": False,
            "created_at": now(),
            "updated_at": now(),
            "name": "New session",
            "context_limit": 40000,
            "commands_allowed": False,
            "archived": False,
            "coverage": "recorded transfers only",
        }
        self.ws.put("session", session["id"], session)
        self.ws.put("run", "chat:" + session["id"], {"task_id": task_id})
        return session

    def get(self, session_id: str) -> dict:
        """Load an explicit conversation binding."""
        if deletion_path(self.ws, session_id).exists():
            raise PilotError("This session was deleted. Select another session or create a new one.", "session_deleted")
        with self.ws.db() as db:
            if db.execute("SELECT 1 FROM records WHERE kind='deleted_session' AND id=?", (session_id,)).fetchone():
                raise PilotError("This session was deleted. Select another session or create a new one.", "session_deleted")
        return {"context_limit": 40000, "commands_allowed": False,
                **self.ws.get("session", session_id)}

    @contextmanager
    def activity(self, session_id: str):
        """Fail promptly during active work; unrelated source indexing never blocks this lease."""
        key = (str(self.ws.state), session_id)
        held = getattr(_ACTIVITIES, "held", None)
        if held is None:
            held = _ACTIVITIES.held = set()
        if key in held:
            self.get(session_id)
            yield
            return
        with ExitStack() as stack:
            try:
                stack.enter_context(self.ws.lock(timeout=0, scope="session:" + session_id))
            except PilotError as exc:
                if exc.code == "busy":
                    raise PilotError("This session is still executing a tool. Stop the command or wait for its result before deleting or changing it.", "session_running") from None
                raise
            self.get(session_id)
            held.add(key)
            try:
                yield
            finally:
                held.remove(key)

    @session_operation
    def configure(self, session_id: str, mode: str | None = None,
                  context_limit: int | None = None, commands_allowed: bool | None = None,
                  project: str | None = None) -> dict:
        """Apply locally authored settings; model tools never dispatch this administrative method."""
        session = self.get(session_id)
        if project is not None:
            if not project.strip() or len(project) > 2000:
                raise PilotError("External project scope must contain 1–2,000 characters", "invalid_project")
            session["project"] = project
        if mode is not None:
            if mode not in {"plan", "ask", "code", "review"}:
                raise PilotError("Choose plan, ask, code or review", "invalid_mode")
            # Legacy CLI/RPC input remains accepted, without creating an active mode.
        if context_limit is not None:
            if isinstance(context_limit, bool) or not 1000 <= context_limit <= 2000000:
                raise PilotError("Context threshold must be 1,000–2,000,000 estimated tokens", "invalid_limit")
            session["context_limit"] = context_limit
        if commands_allowed is not None:
            if not isinstance(commands_allowed, bool):
                raise PilotError("Command permission must be a boolean", "invalid_permission")
            session["commands_allowed"] = commands_allowed
        self.ws.put("session", session_id, session)
        return session

    def list(self, query: str = "", archived: bool = False) -> list[dict]:
        """Search local session names, identifiers, and task objectives without source work."""
        tasks = {t["id"]: t["objective"] for t in TaskStore(self.ws).list()}
        rows = []
        deleted = {s["id"] for s in self.ws.records("deleted_session")}
        for session in self.ws.records("session"):
            if session["id"] in deleted or deletion_path(self.ws, session["id"]).exists():
                continue
            row = {**session, "objective": tasks.get(session.get("task_id"), "")}
            if bool(row.get("archived")) == archived and query.casefold() in str(row).casefold():
                rows.append(row)
        return sorted(rows, key=lambda r: r.get("updated_at", r["created_at"]), reverse=True)

    @session_operation
    def update(self, session_id: str, name: str | None = None, archived: bool | None = None) -> dict:
        """Rename or archive one chat without changing its task or executing prior content."""
        session = self.get(session_id)
        if name is not None:
            if not name.strip() or len(name) > 160:
                raise PilotError("Session names must contain 1–160 characters", "invalid_name")
            session["name"] = name.strip()
        if archived is not None:
            session["archived"] = archived
        session["updated_at"] = now()
        self.ws.put("session", session_id, session)
        return session

    @session_operation
    def bind(self, session_id: str, task_id: str) -> dict:
        """Confirm TODO initialization for the selected task in this chat."""
        TaskStore(self.ws).get(task_id)
        session = self.get(session_id)
        if session["task_id"] and session["task_id"] != task_id:
            raise PilotError("Start a new local chat before changing tasks", "wrong_task")
        self.ws.put("run", "chat:" + session_id, {"task_id": task_id})
        session.update(task_id=task_id, todo_ready=True)
        self.ws.put("session", session_id, session)
        return session

    def guard(self, session_id: str, operation: str = "read", override_reason=None) -> dict:
        """Require TODO initialization without imposing conversation length cutoffs."""
        session = self.get(session_id)
        if not session["todo_ready"]:
            raise PilotError("Call todo to start or resume this task first", "todo_required")
        return session

    def record(self, session_id: str, event_id: str, kind: str, text: str) -> None:
        """Record a confirmed transfer exactly once without storing full conversation text."""
        self.get(session_id)
        value = {"kind": kind, "at": now(), "hash": digest(text), **measure(text)}
        with self.ws.db() as db:
            db.execute("INSERT OR IGNORE INTO events VALUES(?,?,?)", (session_id, event_id, canonical(value)))

    def metrics(self, session_id: str) -> dict:
        """Report observed estimates separately from unknown remote context usage."""
        import json

        with self.ws.db() as db:
            events = [
                json.loads(r[0])
                for r in db.execute("SELECT value FROM events WHERE session=? ORDER BY rowid", (session_id,))
            ]
        exchanges = 0
        awaiting = False
        for event in events:
            if event["kind"] == "outgoing":
                awaiting = True
            elif event["kind"] == "model_reply" and awaiting:
                exchanges += 1
                awaiting = False
        total = sum(e["estimated_tokens"] for e in events)
        limit = self.get(session_id)["context_limit"]
        return {
            "observed_estimated_tokens": total,
            "events": len(events),
            "exchanges": exchanges,
            "confidence": "low",
            "tracking_complete": False,
            "warning": "Remote context usage is unknown; unrecorded messages are excluded.",
            "context_limit": limit,
            "estimated_budget_ratio": round(total / limit, 4),
            "handoff_recommended": total >= limit,
        }
