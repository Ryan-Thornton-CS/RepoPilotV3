"""Deduplicated manual JSON dispatch with automatic local conversation bindings."""

from __future__ import annotations

import json

from repopilotv3.core import PilotError, atomic_bytes, canonical, digest, uid
from repopilotv3.exports.manager import ExportManager
from repopilotv3.manual.patches import PatchManager
from repopilotv3.manual.schema import parse
from repopilotv3.manual.sessions import SessionStore, session_operation
from repopilotv3.manual.validation import ValidationEvidence, validate
from repopilotv3.repository.index import changes
from repopilotv3.repository.service import RepositoryTools, bounded
from repopilotv3.tasks.store import TaskStore


class ManualBridge:
    """Execute only registered operations and persist responses before clipboard transfer."""

    def __init__(self, ws, notify=None):
        """Share task and repository services with the MCP interface."""
        self.ws = ws
        self.notify = notify
        self.tools = RepositoryTools(ws)
        self.sessions = SessionStore(ws)
        from repopilotv3.workflow.history import HistoryStore

        self.history = HistoryStore(ws)

    @session_operation
    def execute(self, session_id: str, text: str, record_message: bool = True) -> dict:
        """Reserve a request once; interrupted effects are never silently replayed."""
        self.sessions.get(session_id)
        try:
            request = parse(text)
        except PilotError as exc:
            response = {
                "protocol_version": "1.0",
                "request_id": None,
                "session_id": session_id,
                "ok": False,
                "error": {"code": exc.code, "message": str(exc)},
                "next": "Return one schema-valid JSON request; consult 07_PROTOCOL_REFERENCE.md.",
            }
            self.save_response(session_id, "invalid:" + digest(text), response)
            return response
        request_id = request["request_id"]
        fingerprint = digest(canonical(request))
        with self.ws.lock():
            with self.ws.db() as db:
                row = db.execute(
                    "SELECT hash,response FROM requests WHERE session=? AND id=?", (session_id, request_id)
                ).fetchone()
                if row:
                    if row[0] != fingerprint:
                        raise PilotError("Request ID was reused with different content", "request_conflict")
                    if row[1] is None:
                        raise PilotError(
                            "Request was interrupted; inspect local state and use a new ID",
                            "request_interrupted",
                        )
                    return json.loads(row[1])
                db.execute("INSERT INTO requests VALUES(?,?,?,NULL)", (session_id, request_id, fingerprint))
            self.history.add(
                session_id, "request", {"text": text, "request_id": request_id}, "request:" + request_id
            )
            if record_message:
                self.sessions.record(session_id, "reply:" + request_id, "model_reply", text)
            try:
                result = self.dispatch(session_id, request["tool"], request["arguments"])
                session = self.sessions.get(session_id)
                if result.get("pending_permission"):
                    managed = {"data": result, "monitoring": {"coverage": "Awaiting local permission; command not executed.", "warnings": []}}
                elif request["tool"] == "batch":
                    managed = {
                        "data": result,
                        "monitoring": {
                            "event_id": uid("batch"),
                            "run_id": "chat:" + session_id,
                            "coverage": "managed batch operations",
                            "warnings": [],
                        },
                    }
                else:
                    managed = self.tools.managed(
                        request["tool"],
                        request["arguments"],
                        result,
                        "chat:" + session_id,
                        success=not ((request["tool"] == "validate" and result.get("validation", {}).get("status") != "passed") or
                                     (request["tool"] == "run_command" and result.get("status") != "completed")),
                    )
                response = {
                    "protocol_version": "1.0",
                    "request_id": request_id,
                    "ok": True,
                    "task_id": session["task_id"],
                    "session_id": session_id,
                    **managed,
                }
            except (PilotError, OSError, ValueError) as exc:
                response = {
                    "protocol_version": "1.0",
                    "request_id": request_id,
                    "ok": False,
                    "session_id": session_id,
                    "error": {"code": getattr(exc, "code", "local_error"), "message": str(exc)},
                    "next": "Correct the request or local configuration and use a new request ID.",
                }
                response["monitoring"] = self.tools.managed(
                    request["tool"],
                    request["arguments"],
                    {"error": response["error"]},
                    "chat:" + session_id,
                    success=False,
                )["monitoring"]
            with self.ws.db() as db:
                db.execute(
                    "UPDATE requests SET response=? WHERE session=? AND id=?",
                    (canonical(response), session_id, request_id),
                )
            self.save_response(session_id, request_id, response)
            return response

    def save_response(self, session_id: str, request_id: str, response: dict) -> str:
        """Persist every response under a safe hash-derived filename."""
        path = self.ws.state / "outbox" / session_id / (digest(request_id)[:24] + ".json")
        atomic_bytes(path, (json.dumps(response, indent=2, ensure_ascii=False) + "\n").encode())
        self.history.add(session_id, "result", response, "result:" + request_id)
        return str(path)

    def dispatch(self, session_id: str, tool: str, arguments: dict) -> dict:
        """Route registered tools; execution permission is controlled only by local management."""
        if tool == "task":
            session = self.sessions.get(session_id)
            supplied = arguments.get("task_id")
            if session["task_id"] and supplied and supplied != session["task_id"]:
                raise PilotError("This chat already has a task; start a new session for independent work", "wrong_task")
            task_id = session["task_id"] or supplied
            if task_id:
                task = TaskStore(self.ws).get(task_id)
                if arguments.get("objective"):
                    task = TaskStore(self.ws).refine(task_id, arguments["objective"])
            else:
                if not arguments.get("objective"):
                    raise PilotError("task.arguments.objective is required to create the chat's task", "task_required")
                task = TaskStore(self.ws).create(arguments["objective"], seeds=arguments.get("seeds"))
            session["task_id"] = task["id"]
            self.ws.put("session", session_id, session)
            if arguments.get("chat_name"):
                self.sessions.update(session_id, name=arguments["chat_name"])
            return {"task": task, "chat_name": self.sessions.get(session_id)["name"],
                    "next": "Call todo with progress.items to initialize this chat's checklist."}
        if tool == "todo":
            session = self.sessions.get(session_id)
            args = dict(arguments)
            supplied = args.get("task_id")
            if session["task_id"]:
                if supplied and supplied != session["task_id"]:
                    raise PilotError("Request task differs from the selected conversation", "wrong_task")
                args["task_id"] = session["task_id"]
            result = self.tools.checkpoint(**args, run_id="chat:" + session_id)
            self.sessions.bind(session_id, result["task"]["id"])
            result["validation_profiles"] = list(self.ws.config["profiles"])
            result["default_validation"] = self.ws.config["default_validation"]
            return result
        session = self.sessions.guard(session_id, tool)
        task_id = session["task_id"]
        if tool in {"run_command", "command_output"}:
            from repopilotv3.manual.commands import CommandRunner

            runner = CommandRunner(self.ws, self.notify)
            return runner.request(session_id, arguments) if tool == "run_command" else runner.output(session_id, **arguments)
        if tool == "search":
            return self.tools.search(**arguments)
        if tool == "read":
            return self.tools.read(**arguments)
        if tool == "context":
            return self.tools.context(**arguments, task=TaskStore(self.ws).get(task_id))
        if tool == "changes":
            snapshot = self.tools.snapshot()["id"]
            return {
                "snapshot": snapshot,
                **changes(self.ws),
                "validation": ValidationEvidence(self.ws).inspect(snapshot, **arguments, task_id=task_id),
            }
        if tool == "propose_patch":
            result = PatchManager(self.ws).stage(arguments, session_id, task_id)
            return {**result, "next": "Review the staged diff locally. Source is unchanged until approval."}
        if tool == "validate":
            record = validate(self.ws, arguments["profile"], task_id)
            return {
                "snapshot": record["snapshot"],
                "validation": ValidationEvidence(self.ws).inspect(record["snapshot"], record["id"]),
            }
        if tool == "handoff":
            exported = ExportManager(self.ws).prepare(session_id)
            return {
                "snapshot": exported["manifest"]["snapshot"],
                "directory": exported["directory"],
                "continuation_file": exported["directory"] + "/06_CONTINUATION.md",
                "bootstrap": (
                    ExportManager(self.ws).bootstrap(
                        session, TaskStore(self.ws).get(task_id), self.tools.index.current()
                    )
                ),
                "checklist": exported["checklist"],
                "next": "Copy the continuation packet, open a fresh external chat, then select New chat locally.",
            }
        if tool == "batch":
            results = []
            for call in arguments["calls"]:
                try:
                    raw = self.dispatch(session_id, call["tool"], call["arguments"])
                    results.append(
                        self.tools.managed(call["tool"], call["arguments"], raw, "chat:" + session_id)
                    )
                except PilotError as exc:
                    results.append(
                        self.tools.managed(
                            call["tool"],
                            call["arguments"],
                            {"error": {"code": exc.code, "message": str(exc)}},
                            "chat:" + session_id,
                            success=False,
                        )
                    )
            return bounded({"results": results}, self.ws.config["response_tokens"])
        raise PilotError("Unknown manual operation", "unknown_tool")
