"""Versioned JSON-RPC 2.0 management over newline-delimited local stdio only."""

from __future__ import annotations

import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from jsonschema import Draft202012Validator

from repopilotv3.core import PilotError, canonical, uid
from repopilotv3.tasks.store import TaskStore
from repopilotv3.workflow.service import WorkflowService

STRING = {"type": "string", "minLength": 1, "maxLength": 2500000}
SESSION = {"session_id": STRING}
METHODS = {
    "state.status": ({}, []),
    "browser.pending": (SESSION, ["session_id"]),
    "browser.notice": ({**SESSION, "text": STRING}, ["session_id", "text"]),
    "browser.demo.start": ({}, []),
    "browser.demo.stop": ({}, []),
    "browser.demo.status": ({}, []),
    "browser.settings": ({**SESSION, "settings": {"type": "object", "additionalProperties": False,
        "properties": {"url": {"type": "string", "maxLength": 4000}, "enabled": {"type": "boolean"},
                       "mode": {"enum": ["fill", "capture", "loop"]},
                       "budget": {"type": "integer", "minimum": 1, "maximum": 100},
                       "selectors": {"type": "object", "additionalProperties": False,
                           "properties": {key: {"type": "string", "maxLength": 2000}
                                          for key in ("input", "send", "reply", "sent", "busy", "complete")}}}}}, []),
    "browser.transfer": ({**SESSION, "transfer_id": STRING,
                         "phase": {"enum": ["inspect", "fill_pending", "filled", "send_pending", "sent", "captured", "imported", "paused"]},
                         "text": STRING}, ["session_id", "transfer_id", "phase"]),
    "harness.preview": ({"repository_root": STRING, "repository_state": STRING, "server_name": STRING}, []),
    "harness.apply": ({"plan_id": STRING}, ["plan_id"]),
    "harness.status": ({}, []),
    "harness.recover": ({"recovery_id": STRING}, ["recovery_id"]),
    "sessions.configure": ({**SESSION, "mode": {"enum": ["plan", "ask", "code", "review"]},
                            "context_limit": {"type": "integer", "minimum": 1000, "maximum": 2000000},
                            "commands_allowed": {"type": "boolean"}, "project": {**STRING, "maxLength": 2000}}, ["session_id"]),
    "composer.preview": ({**SESSION, "text": STRING}, ["session_id", "text"]),
    "composer.submit": ({**SESSION, "text": STRING}, ["session_id", "text"]),
    "prompts.read": ({"role": {"enum": ["planning", "asking", "implementation", "review"]}}, ["role"]),
    "prompts.reset": ({"role": {"enum": ["planning", "asking", "implementation", "review"]}, "expected_hash": STRING}, ["role", "expected_hash"]),
    "todo.reset": ({**SESSION, "expected_revision": {"type": "integer", "minimum": 0}}, ["session_id", "expected_revision"]),
    "prompts.starter": (SESSION, ["session_id"]),
    "prompts.project": ({}, []),
    "prompts.compose": ({**SESSION, "text": STRING}, ["session_id", "text"]),
    "replies.import": ({**SESSION, "text": STRING}, ["session_id", "text"]),
    "replies.preview": ({**SESSION, "text": STRING}, ["session_id", "text"]),
    "replies.status": ({**SESSION, "reply_id": STRING}, ["session_id", "reply_id"]),
    "replies.resolve": ({**SESSION, "reply_id": STRING, "approved": {"type": "boolean"}, "profile": STRING},
                        ["session_id", "reply_id", "approved"]),
    "commands.cancel": ({**SESSION, "command_id": STRING}, ["session_id", "command_id"]),
    "commands.diffs": ({**SESSION, "command_id": STRING, "path": STRING}, ["session_id", "command_id"]),
    "hello": ({}, []),
    "setup": ({"validation": {"enum": ["keep", "python", "dotnet_test", "dotnet_build"]}}, []),
    "diagnostics": ({}, []),
    "sessions.list": ({"query": STRING, "archived": {"type": "boolean"}}, []),
    "sessions.create": ({"name": STRING, "task_id": STRING, "objective": STRING, "project": STRING}, []),
    "sessions.select": (SESSION, ["session_id"]),
    "sessions.update": ({**SESSION, "name": STRING, "archived": {"type": "boolean"}}, ["session_id"]),
    "sessions.delete": (SESSION, ["session_id"]),
    "sessions.status": (SESSION, ["session_id"]),
    "sessions.selected": ({}, []),
    "tasks.list": ({}, []),
    "history.list": (
        {
            **SESSION,
            "before": {"type": "integer", "minimum": 1},
            "limit": {"type": "integer", "minimum": 1, "maximum": 100},
        },
        ["session_id"],
    ),
    "history.copy_turn": ({**SESSION, "entry_id": STRING}, ["session_id", "entry_id"]),
    "draft.save": ({**SESSION, "text": {"type": "string", "maxLength": 2500000}}, ["session_id", "text"]),
    "notes.add": ({**SESSION, "text": STRING}, ["session_id", "text"]),
    "todo.update": ({**SESSION, "progress": {"type": "object"}}, ["session_id", "progress"]),
    "manual.execute": ({**SESSION, "text": STRING}, ["session_id", "text"]),
    "patch.review": ({**SESSION, "patch_id": STRING, "path": STRING}, ["session_id", "patch_id"]),
    "patch.approve": (
        {**SESSION, "patch_id": STRING, "approved": {"type": "boolean"}, "profile": STRING},
        ["session_id", "patch_id", "approved"],
    ),
    "patch.recover": ({**SESSION, "patch_id": STRING}, ["session_id", "patch_id"]),
    "exports.prepare": ({**SESSION, "full_source": {"type": "boolean"}, "background": {"type": "boolean"}}, ["session_id"]),
    "transfers.confirm": (
        {
            **SESSION,
            "generation_id": STRING,
            "confirmations": {"type": "object", "additionalProperties": STRING},
        },
        ["session_id", "generation_id", "confirmations"],
    ),
    "transfers.record": ({**SESSION, "text": STRING}, ["session_id", "text"]),
    "handoff": ({**SESSION, "confirmed_new_chat": {"type": "boolean"}}, ["session_id"]),
}
# Slow repository operations share one executor; cheap state/history reads remain responsive.
SLOW = {
    "composer.submit", "prompts.read", "prompts.reset", "todo.reset",
    "harness.preview", "harness.apply", "harness.recover",
    "prompts.starter", "prompts.compose", "replies.import", "replies.resolve", "commands.diffs",
    "setup",
    "manual.execute",
    "patch.review",
    "patch.approve",
    "patch.recover",
    "exports.prepare",
    "handoff",
    "todo.update",
    "transfers.confirm",
}
ADMIN = {"sessions.delete", "sessions.update"}


class BridgeServer:
    """Serve trusted local management separately from the model-callable MCP/manual registries."""

    def __init__(self, ws, output=None):
        """Bind a single workspace and create no session or external process implicitly."""
        self.ws = ws
        self.output = output or sys.stdout
        self.output_lock = threading.Lock()
        from repopilotv3.browser.demo import DemoServer
        from repopilotv3.storage.migration import StateMigration

        self.demo = DemoServer()
        self.service = WorkflowService(ws, self.command_progress, defer_exports=True) if ws.config_path.exists() and not StateMigration(ws).status()["recovery_required"] else None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="repopilot-work")
        self.admin = ThreadPoolExecutor(max_workers=1, thread_name_prefix="repopilot-sessions")
        self.inflight = set()
        self.ids_lock = threading.Lock()
        from repopilotv3.manual.commands import CommandRunner

        CommandRunner.closing_states.discard(str(ws.state))

    def command_progress(self, value: dict) -> None:
        """Stream bounded command output/status using the same local notification channel."""
        self.emit({"jsonrpc": "2.0", "method": "progress", "params": {"method": "command", **value}})

    def emit(self, value: dict) -> None:
        """Write exactly one UTF-8 JSON frame, keeping logging off protocol stdout."""
        with self.output_lock:
            self.output.write(canonical(value) + "\n")
            self.output.flush()

    def validate(self, message: dict) -> tuple[str, dict]:
        """Reject unknown methods, extra arguments, mismatched roots, and unsupported versions."""
        if (
            not isinstance(message, dict)
            or message.get("jsonrpc") != "2.0"
            or set(message) != {"jsonrpc", "id", "method", "params"}
            or not isinstance(message["id"], (str, int))
            or isinstance(message["id"], bool)
        ):
            raise PilotError("Expected a JSON-RPC 2.0 request with id, method, and params", "invalid_request")
        method = message["method"]
        if not isinstance(method, str) or method not in METHODS:
            raise PilotError("Unknown management method", "method_not_found")
        params = message["params"]
        if isinstance(params, dict) and isinstance(params.get("workspace"), str):
            try:
                if Path(params["workspace"]).is_absolute() and Path(params["workspace"]).resolve() == self.ws.root:
                    params = {**params, "workspace": str(self.ws.root)}
            except (OSError, ValueError):
                pass
        properties, required = METHODS[method]
        schema = {
            "type": "object",
            "properties": {
                "workspace": {"const": str(self.ws.root)},
                "bridge_version": {"const": "1.0"},
                **properties,
            },
            "required": ["workspace", "bridge_version", *required],
            "additionalProperties": False,
        }
        errors = list(Draft202012Validator(schema).iter_errors(params))
        if errors:
            raise PilotError("Invalid management arguments: " + errors[0].message[:1000], "invalid_params")
        return method, {k: v for k, v in params.items() if k not in {"workspace", "bridge_version"}}

    def call(self, method: str, p: dict):
        """Dispatch closed management methods; none accepts shell commands or harness writes."""
        if method == "state.status":
            from repopilotv3.storage.migration import StateMigration
            return StateMigration(self.ws).status()
        if method.startswith("browser.demo."):
            return getattr(self.demo, method.rsplit(".", 1)[1])()
        if method == "hello":
            from repopilotv3 import __version__

            return {
                "bridge_version": "1.0",
                "backend_version": __version__,
                "workspace": str(self.ws.root),
                "initialized": self.service is not None,
                "methods": list(METHODS),
            }
        if method == "setup":
            from repopilotv3.cli import configure_profile

            self.ws.initialize()
            with self.ws.lock():
                configure_profile(self.ws, p.get("validation", "keep"))
            self.service = WorkflowService(self.ws, self.command_progress, defer_exports=True)
            return {"configured": True, "config": str(self.ws.config_path), "harness_written": False}
        if self.service is None:
            raise PilotError("Run setup to initialize private state first", "not_initialized")
        service = self.service
        if method == "browser.pending":
            service.check(p["session_id"])
            return [record for record in self.ws.records("browser_transfer")
                    if record["session_id"] == p["session_id"] and record["phase"] == "captured"][-1:]
        if method == "browser.notice":
            service.check(p["session_id"])
            service.history.add(p["session_id"], "local", {"action": "browser_relay", "text": p["text"]})
            return {"saved": True}
        if method in {"browser.settings", "browser.transfer"}:
            from repopilotv3.browser.settings import BrowserSettings
            browser = BrowserSettings(service)
            return browser.configure(**p) if method == "browser.settings" else browser.transfer(**p)
        if method.startswith("harness."):
            from repopilotv3.integrations.harness import HarnessManager

            manager = HarnessManager(self.ws)
            if method == "harness.status":
                return manager.status()
            with self.ws.lock():
                return getattr(manager, method.split('.')[1])(**p)
        if method in {"prompts.read", "prompts.reset"}:
            from repopilotv3.workflow.roles import RolePrompts

            return getattr(RolePrompts(self.ws), method.split(".")[1])(**p)
        if method == "prompts.project":
            from repopilotv3.exports.instructions import ProjectInstructions

            return ProjectInstructions().describe()
        if "session_id" in p and method != "sessions.delete":
            service.sessions.get(p["session_id"])
        if method == "composer.preview":
            return service.submission_preview(p["text"])
        if method == "composer.submit":
            return service.submit(**p)
        if method == "todo.reset":
            return service.reset_checklist(**p)
        if method == "sessions.configure":
            return service.configure(**p)
        if method == "prompts.starter":
            return service.starter(**p)
        if method == "prompts.compose":
            return service.compose(**p)
        if method == "replies.import":
            return service.import_reply(**p)
        if method == "replies.preview":
            from repopilotv3.workflow.replies import parse_reply

            _, requests = parse_reply(p["text"])
            return {"has_commands": any(r["tool"] == "run_command" for r in requests)}
        if method == "replies.status":
            return service.reply_status(**p)
        if method == "replies.resolve":
            return service.resolve_reply(**p)
        if method.startswith("commands."):
            from repopilotv3.manual.commands import CommandRunner

            runner = CommandRunner(self.ws, self.command_progress)
            return runner.cancel(**p) if method == "commands.cancel" else runner.diffs(**p)
        if method == "diagnostics":
            return service.diagnostics()
        if method == "sessions.list":
            return service.sessions.list(**p)
        if method == "sessions.create":
            return service.create(**p)
        if method == "sessions.select":
            return service.select(**p)
        if method == "sessions.update":
            return service.sessions.update(**p)
        if method == "sessions.delete":
            return service.delete(**p)
        if method == "sessions.status":
            return service.status(**p)
        if method == "sessions.selected":
            return {"session_id": service.history.preference("selected_session")}
        if method == "tasks.list":
            return TaskStore(self.ws).list()
        if method == "history.list":
            return service.history.page(p.pop("session_id"), **p)
        if method == "history.copy_turn":
            from repopilotv3.workflow.turns import ConversationTurns

            return ConversationTurns(service.history).copy(p["session_id"], p["entry_id"])
        if method == "draft.save":
            service.history.preference("draft:" + p["session_id"], p["text"])
            return {"saved": True}
        if method == "notes.add":
            return service.note(**p)
        if method == "todo.update":
            return service.progress(**p)
        if method == "manual.execute":
            return service.execute(**p)
        if method == "patch.review":
            return service.review(**p, metadata_only="path" not in p)
        if method == "patch.approve":
            return service.approve(**p)
        if method == "patch.recover":
            return service.recover(**p)
        if method == "exports.prepare":
            background = p.pop("background", False)
            try:
                return service.prepare(**p, lock_timeout=0 if background else 30)
            except PilotError as exc:
                if background and exc.code == "busy":
                    return {"deferred": True, "reason": "writer_busy"}
                raise
        if method == "transfers.confirm":
            return service.confirm(**p)
        if method == "transfers.record":
            return service.transfer(**p)
        if method == "handoff":
            return service.handoff(**p)
        raise PilotError("Unknown management method", "method_not_found")

    def respond(self, message: dict, method: str, params: dict) -> None:
        """Return a single correlated result; failures never retry potentially mutating work."""
        started = time.perf_counter()
        token = uid("job")
        try:
            if method in SLOW | ADMIN:
                self.emit(
                    {
                        "jsonrpc": "2.0",
                        "method": "progress",
                        "params": {
                            "token": token,
                            "request_id": message["id"],
                            "method": method,
                            "state": "started",
                        },
                    }
                )
            result = self.call(method, params)
            self.emit({"jsonrpc": "2.0", "id": message["id"], "result": result})
        except Exception as exc:
            refresh_only = method in {"exports.prepare", "prompts.starter", "patch.review", "commands.diffs", "replies.preview", "history.copy_turn", "harness.preview", "harness.status"}
            safe_retry = refresh_only and getattr(exc, "code", None) == "busy"
            if self.service and params.get("session_id") and not safe_retry:
                try:
                    self.service.sessions.get(params["session_id"])
                    self.service.history.add(
                        params["session_id"],
                        "local",
                        {
                            "action": "operation_failed",
                            "method": method,
                            "effects_may_be_uncertain": not refresh_only,
                            "error": {"code": getattr(exc, "code", "local_error"), "message": str(exc)},
                        },
                    )
                except Exception:
                    # History failure must not prevent the operational error frame.
                    pass
            self.emit(
                {
                    "jsonrpc": "2.0",
                    "id": message["id"],
                    "error": {
                        "code": -32000,
                        "message": str(exc),
                        "data": {
                            "code": getattr(exc, "code", "local_error"),
                            "replay_safe": safe_retry,
                            "next": "Wait for the current repository operation, then retry the refresh." if safe_retry else "Inspect session history and source before retrying.",
                        },
                    },
                }
            )
        finally:
            elapsed = time.perf_counter() - started
            if elapsed >= 0.5:
                print(f"Slow local handler: {method} took {elapsed * 1000:.0f} ms (excludes queue time).", file=sys.stderr, flush=True)
            if method in SLOW | ADMIN:
                self.emit(
                    {
                        "jsonrpc": "2.0",
                        "method": "progress",
                        "params": {
                            "token": token,
                            "request_id": message["id"],
                            "method": method,
                            "state": "finished",
                        },
                    }
                )
            with self.ids_lock:
                self.inflight.discard(message["id"])

    def serve(self, input_=None) -> None:
        """Read bounded frames while serializing repository jobs; EOF drains accepted work."""
        stream = input_ or sys.stdin
        try:
            while True:
                line = stream.readline(8 * 1024 * 1024 + 1)
                if not line:
                    break
                message = None
                try:
                    if len(line.encode("utf-8")) > 8 * 1024 * 1024:
                        # Drain an oversized frame so a suffix cannot become a second request.
                        while line and not line.endswith("\n"):
                            line = stream.readline(8 * 1024 * 1024 + 1)
                        raise PilotError("Management frame exceeds 8 MiB", "invalid_request")
                    message = json.loads(line)
                    method, params = self.validate(message)
                    with self.ids_lock:
                        if message["id"] in self.inflight:
                            raise PilotError("RPC id is already in flight", "request_conflict")
                        self.inflight.add(message["id"])
                    if method in ADMIN:
                        self.admin.submit(self.respond, message, method, params)
                    elif method in SLOW:
                        self.executor.submit(self.respond, message, method, params)
                    else:
                        self.respond(message, method, params)
                except (ValueError, PilotError, RecursionError) as exc:
                    self.emit(
                        {
                            "jsonrpc": "2.0",
                            "id": message.get("id") if isinstance(message, dict) else None,
                            "error": {
                                "code": -32600,
                                "message": str(exc),
                                "data": {"code": getattr(exc, "code", "invalid_request")},
                            },
                        }
                    )
        finally:
            from repopilotv3.manual.commands import CommandRunner

            self.demo.stop()
            CommandRunner(self.ws).cancel_all()
            self.executor.shutdown(wait=True, cancel_futures=False)
            self.admin.shutdown(wait=True, cancel_futures=False)
