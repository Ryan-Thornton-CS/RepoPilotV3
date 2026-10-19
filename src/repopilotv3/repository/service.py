"""Bounded shared repository retrieval and persistent Copilot task checkpoints."""

from __future__ import annotations

import copy
import sqlite3

from repopilotv3.core import PilotError, canonical, digest, uid
from repopilotv3.integrations.monitor import Monitor
from repopilotv3.manual.schema import PROGRESS, validate
from repopilotv3.manual.sessions import measure
from repopilotv3.repository.index import RepositoryIndex
from repopilotv3.tasks.store import TaskStore


def bounded(value: dict, budget: int = 4000) -> dict:
    """Reduce large lists or complete text lines, retaining hashes and explicit omissions."""
    result = copy.deepcopy(value)
    omissions = result.setdefault("omissions", [])
    while measure(canonical(result))["estimated_tokens"] > budget:
        choices = []
        stack = [(result, "")]
        while stack:
            node, trail = stack.pop()
            if isinstance(node, dict):
                for key, child in node.items():
                    if key in {"omissions", "monitoring", "snapshot", "hash", "base_hashes"}:
                        continue
                    if isinstance(child, (list, str)) and len(child) > 0:
                        choices.append((len(canonical(child)), node, key, trail + "/" + key))
                    if isinstance(child, dict):
                        stack.append((child, trail + "/" + key))
        if not choices:
            if len(omissions) > 1:
                total = len(omissions)
                omissions[:] = [{"reason": f"{total} omitted fields; narrow the query or explicit paths."}]
                continue
            break
        _, parent, key, trail = max(choices, key=lambda x: x[0])
        child = parent[key]
        if isinstance(child, list):
            parent[key] = child[: len(child) // 2]
        else:
            lines = child.splitlines(keepends=True)
            parent[key] = "".join(lines[: len(lines) // 2])
            if key == "text" and "start" in parent:
                parent["end"] = parent["start"] + len(parent[key].splitlines()) - 1
        if not any(o.get("field") == trail for o in omissions):
            omissions.append(
                {
                    "field": trail,
                    "reason": "response budget",
                    "next": "Request a smaller source range or focused query.",
                }
            )
    return result


class RepositoryTools:
    """Supply the same retrieval behavior to MCP and the manual dispatcher."""

    def __init__(self, ws):
        """Reuse one workspace index rather than building a frontend-specific index."""
        self.ws = ws
        self.index = RepositoryIndex(ws)

    def snapshot(self):
        """Refresh captured source before answering a repository request."""
        return self.index.refresh()

    def search(self, query: str, limit: int = 12) -> dict:
        """Find keyword matches, or list bounded eligible paths with query '*'."""
        snap = self.snapshot()
        limit = max(1, min(limit, 50))
        if query.strip() == "*":
            selected = sorted(snap["hashes"])[:limit]
            result = {"snapshot": snap["id"], "quality": snap["quality"], "symbols": [],
                      "hits": [{"path": path, "hash": snap["hashes"][path]} for path in selected],
                      "search_kind": "file_list", "remaining_files": max(0, len(snap["hashes"]) - len(selected))}
        else:
            result = {**self.index.query(query, limit, snap), "search_kind": "keyword"}
        result["indexed_file_count"] = len(snap["hashes"])
        if not result["hits"] and not result["symbols"]:
            result["next"] = ("No eligible indexed files; the selected folder may be empty or excluded. Use this snapshot for new-file proposals with null base hashes."
                              if not snap["hashes"] else "No keyword matches. Use short identifiers or query '*' to list eligible paths.")
        symbols = {s["id"] for s in result["symbols"]}
        result["relationships"] = [
            r for r in snap["relationships"] if r["source"] in symbols or r["target"] in symbols
        ][:50]
        return result

    def read(self, path: str, start: int = 1, end: int = 200) -> dict:
        """Return captured source bytes rendered as a cited line range."""
        return self.index.source(path, start, end, self.snapshot())

    def context(self, query: str = "", paths: list | None = None, task: dict | None = None) -> dict:
        """Assemble complete bounded files plus declarations and expansion hints."""
        snap = self.snapshot()
        task = task or {"objective": query, "pins": [], "seeds": paths or [], "excludes": []}
        if query:
            task = {**task, "objective": query}
        cache_key = digest(canonical([snap["id"], paths, task, self.ws.config["response_tokens"]]))
        try:
            return self.ws.get("context_cache", cache_key)
        except PilotError as exc:
            if exc.code != "not_found":
                raise
        selected = list(dict.fromkeys((paths or []) + self.index.select(task, snap)))
        files = self.index.files(snap["id"])
        result = {
            "snapshot": snap["id"],
            "indexed_file_count": len(snap["hashes"]),
            "quality": snap["quality"],
            "diagnostics": snap["diagnostics"][:10],
            "sources": [],
            "symbols": [],
            "relationships": [],
            "omissions": [],
        }
        for path in selected:
            if path not in files:
                raise PilotError(f"Path not indexed: {path}", "not_found")
            text = files[path].decode("utf-8-sig")
            entry = {
                "path": path,
                "start": 1,
                "end": len(text.splitlines()),
                "hash": snap["hashes"][path],
                "text": text,
            }
            if (
                measure(canonical(result) + canonical(entry))["estimated_tokens"]
                > self.ws.config["response_tokens"] - 500
            ):
                result["omissions"].append(
                    {
                        "path": path,
                        "hash": snap["hashes"][path],
                        "reason": "complete file exceeds remaining budget",
                        "next": f"read {path} with a bounded line range",
                    }
                )
            else:
                result["sources"].append(entry)
        ids = {s["id"] for s in snap["symbols"] if s["path"] in selected}
        result["symbols"] = [s for s in snap["symbols"] if s["id"] in ids][:50]
        result["relationships"] = [
            r for r in snap["relationships"] if r["source"] in ids or r["target"] in ids
        ][:50]
        self.ws.put("context_cache", cache_key, result)
        return result

    def checkpoint(
        self,
        objective: str | None = None,
        task_id: str | None = None,
        progress: dict | None = None,
        seeds: list | None = None,
        run_id: str | None = None,
    ) -> dict:
        """Persist unverified progress without executing code or accepting validation claims."""
        validate(progress or {}, PROGRESS)
        tasks = TaskStore(self.ws)
        if task_id:
            task = tasks.get(task_id)
            if objective and objective != task["objective"]:
                raise PilotError("Resume with the existing objective or create another task", "wrong_task")
        elif objective:
            task = tasks.create(objective, seeds=seeds)
        else:
            raise PilotError("Supply objective to start or task_id to resume", "task_required")
        if run_id:
            run = self.ws.get("run", run_id)
            if run["task_id"] not in {None, task["id"]}:
                raise PilotError("Work run belongs to another task", "wrong_task")
        else:
            run_id = uid("run")
        self.ws.put("run", run_id, {"task_id": task["id"]})
        if progress:
            task = tasks.update(task["id"], progress)
        try:
            monitoring = Monitor(self.ws).checkpoint(task, run_id)
        except (sqlite3.Error, OSError):
            monitoring = {"coverage": "degraded", "warnings": []}
        return {
            "task": task,
            "run_id": run_id,
            "todo": tasks.render(task),
            "monitoring": monitoring,
            "next": "Continue with the next TODO item; checkpoint after meaningful progress.",
        }

    def managed(
        self, tool: str, args: dict, result: dict, task_id: str | None = None, success: bool = True
    ) -> dict:
        """Bound results and attach best-effort warning metadata to managed retrieval."""
        if task_id and not task_id.startswith("chat:"):
            self.ws.get("run", task_id)
        body = bounded(result, self.ws.config["response_tokens"] - 250)
        try:
            monitor = Monitor(self.ws).record(
                task_id or "unattributed", tool, args, body, result.get("snapshot", ""), success
            )
        except (sqlite3.Error, OSError):
            monitor = {"coverage": "degraded", "warnings": []}
        return {"data": body, "monitoring": monitor}
