"""Persistent task state. Model assertions never become local validation facts."""

from __future__ import annotations

from repopilotv3.core import (
    PilotError,
    Workspace,
    atomic_bytes,
    atomic_json,
    identifier,
    load_json,
    now,
    uid,
)


class TaskStore:
    """Keep authoritative structured progress and its human-readable TODO projection."""

    def __init__(self, ws: Workspace):
        """Attach task persistence to one workspace."""
        self.ws = ws

    def path(self, task_id: str):
        """Return the authoritative JSON path for a validated task identifier."""
        return self.ws.state / "tasks" / identifier(task_id) / "task-state.json"

    def create(self, objective: str, task_id: str | None = None, seeds: list | None = None) -> dict:
        """Register a new objective, source seeds, and stable task identity."""
        task_id = identifier(task_id or uid("task"))
        if not objective.strip() or len(objective) > 20000:
            raise PilotError("Task objective must contain 1–20,000 characters")
        if self.path(task_id).exists():
            raise PilotError("Task already exists", "conflict")
        task = {
            "schema_version": "1.0",
            "id": task_id,
            "objective": objective,
            "seeds": seeds or [],
            "pins": [],
            "excludes": [],
            "items": [],
            "errors": [],
            "files": [],
            "decisions": [],
            "questions": [],
            "assumptions": [],
            "required_tests": [],
            "validation": [],
            "created_at": now(),
            "updated_at": now(),
            "revision": 0,
            "checkpoint_required": False,
            "last_progress": None,
        }
        self.save(task)
        return task

    def get(self, task_id: str | None = None) -> dict:
        """Read authoritative task JSON for an explicit task identifier."""
        if task_id is None:
            raise PilotError("An explicit task ID is required", "task_required")
        task = load_json(self.path(task_id))
        if not task:
            raise PilotError(f"Unknown task: {task_id}", "not_found")
        return task

    def list(self) -> list[dict]:
        """Enumerate authoritative task JSON, tolerating an interrupted SQLite projection update."""
        base = self.ws.state / "tasks"
        return [self.get(p.parent.name) for p in sorted(base.glob("*/task-state.json"))]

    def save(self, task: dict) -> None:
        """Atomically persist task JSON and refresh the TODO and searchable record."""
        path = self.path(task["id"])
        atomic_json(path, task)
        self.ws.put("task", task["id"], task)
        atomic_bytes(path.parent / "05_TODO.md", self.render(task).encode())

    def update(self, task_id: str, update: dict, origin: str = "model") -> dict:
        """Merge reported progress without removing requirements or overwriting validation."""
        permitted = {"items", "errors", "files", "decisions", "questions", "assumptions", "required_tests"}
        if set(update) - permitted:
            raise PilotError(
                "Progress updates cannot change objectives, source state, or validation facts",
                "invalid_progress",
            )
        task = self.get(task_id)
        items = {x["id"]: x for x in task["items"]}
        for item in update.get("items", []):
            if set(item) - {"id", "text", "status"} or not {"id", "text", "status"} <= set(item):
                raise PilotError("Items require only id, text, and status", "invalid_progress")
            identifier(item["id"])
            if (
                item["status"] not in {"pending", "active", "blocked", "completed"}
                or not item["text"].strip()
            ):
                raise PilotError("Invalid progress item", "invalid_progress")
            previous = items.get(item["id"])
            if previous and any(previous.get(key) != item[key] for key in ("text", "status")):
                task.setdefault("history", []).append({"at": now(), "origin": origin,
                                                       "item": dict(previous)})
            items[item["id"]] = {**item, "reported_by": origin, "verification": "unverified"}
        task["items"] = list(items.values())
        for key in permitted - {"items"}:
            values = update.get(key, [])
            if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
                raise PilotError(f"{key} must be a list of strings", "invalid_progress")
            task[key] = list(dict.fromkeys(task[key] + values))
        task.update(
            updated_at=now(),
            revision=task["revision"] + 1,
            checkpoint_required=False,
            last_progress={"at": now(), "origin": origin},
        )
        self.save(task)
        return task

    def refine(self, task_id: str, objective: str) -> dict:
        """Revise one evolving objective while retaining previous wording and all requirements."""
        if not objective.strip() or len(objective) > 20000:
            raise PilotError("Task objective must contain 1–20,000 characters", "invalid_objective")
        task = self.get(task_id)
        if task["objective"] != objective:
            task.setdefault("history", []).append({"at": now(), "origin": "model",
                                                   "objective": task["objective"]})
            task.update(objective=objective, revision=task["revision"] + 1, updated_at=now())
            self.save(task)
        return task

    def render(self, task: dict) -> str:
        """Render progress, constraints, and local validation IDs as a Markdown checklist."""
        text = f"# Task: {task['id']}\n\n{task['objective']}\n\n"
        for item in task["items"]:
            mark = "x" if item["status"] == "completed" else " "
            text += f"- [{mark}] `{item['id']}` {item['text']} — {item['status']}; {item['reported_by']}, unverified\n"
        for key in ["errors", "files", "decisions", "questions", "assumptions", "required_tests"]:
            text += f"\n## {key.replace('_', ' ').title()}\n\n"
            text += "\n".join(f"- {x}" for x in task[key]) + "\n"
        text += "\n## Local validation records\n\n"
        text += "\n".join(f"- {x}" for x in task["validation"]) or "No local validation recorded."
        text += "\n\nCompleted items are reported progress; only local validation records establish build/test results.\n"
        return text
