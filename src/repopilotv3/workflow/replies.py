"""Whole WebUI reply parsing, sequential tool execution and durable approval continuations."""

from __future__ import annotations

import json
import re

from repopilotv3.core import PilotError, canonical, digest
from repopilotv3.manual.bridge import ManualBridge
from repopilotv3.manual.commands import CommandRunner
from repopilotv3.manual.schema import parse
from repopilotv3.workflow.prompts import PromptService
from repopilotv3.workflow.formatting import is_tool_response


def json_values(text: str) -> list:
    """Decode adjacent JSON values or an array without treating surrounding prose as code."""
    decoder = json.JSONDecoder()
    values = []
    offset = 0
    while offset < len(text):
        while offset < len(text) and text[offset].isspace():
            offset += 1
        if offset == len(text):
            break
        try:
            value, offset = decoder.raw_decode(text, offset)
        except (ValueError, RecursionError) as exc:
            raise PilotError(f"Invalid JSON: {exc}", "invalid_request") from exc
        values.extend(value if isinstance(value, list) else [value])
        if len(values) > 8:
            raise PilotError("Use at most eight requests per reply", "invalid_request")
    return values


def tool_request(value) -> dict:
    """Reject accidentally returned harness output with a short, non-recursive corrective message."""
    if is_tool_response(value):
        raise PilotError("This is a RepoPilot tool response. Paste it into the WebUI model, then return its next tool requests here. No tools ran.", "response_pasted")
    return parse(canonical(value))


def parse_reply(text: str) -> tuple[str, list[dict]]:
    """Validate every explicit request before effects, including adjacent objects in one fence."""
    if len(text.encode("utf-8")) > 2500000:
        raise PilotError("Reply exceeds 2.5 MB", "invalid_request")
    stripped = text.strip()
    if not stripped:
        raise PilotError("Paste a model reply or a complete JSON tool request first", "invalid_request")
    requests, spans = [], []
    if stripped.startswith("{") or re.match(r"\[\s*(?:\{|\])", stripped):
        requests = [tool_request(value) for value in json_values(stripped)]
        if not requests:
            raise PilotError("Return at least one JSON tool request", "invalid_request")
        prose = ""
    else:
        pattern = r"^```(?:json|repopilot)?[ \t]*\r?\n(.*?)^```[ \t]*\r?$"
        matches = list(re.finditer(pattern, text, re.M | re.S))
        residual = text
        for match in reversed(matches):
            residual = residual[:match.start()] + residual[match.end():]
        if re.search(r'^```(?:json|repopilot)[^\n]*\n.*"(?:tool|protocol_version|request_id)"\s*:', residual, re.M | re.S):
            raise PilotError("Tool code fence is incomplete; return a complete fenced JSON request", "invalid_request")
        for match in matches:
            body = match.group(1).strip()
            try:
                values = json_values(body)
            except PilotError:
                if re.search(r'"(?:tool|protocol_version|request_id)"\s*:', body):
                    raise
                continue
            if any(is_tool_response(value) for value in values):
                raise PilotError("This is a RepoPilot tool response. Paste it into the WebUI model, then return its next tool requests here. No tools ran.", "response_pasted")
            if any(isinstance(value, dict) and any(key in value for key in ("tool", "protocol_version", "request_id")) for value in values):
                requests.extend(tool_request(value) for value in values)
                spans.append(match.span())
        prose = text
        for first, last in reversed(spans):
            prose = prose[:first] + prose[last:]
        if not requests and re.search(r'^```(?:json|repopilot).*"tool"', text, re.M | re.S):
            raise PilotError("Tool code fence is incomplete; return a complete fenced JSON request", "invalid_request")
    if len(requests) > 8:
        raise PilotError("Use at most eight requests per reply", "invalid_request")
    seen = set()
    for request in requests:
        key = request["request_id"]
        if key in seen:
            raise PilotError("Duplicate request ID inside the reply: " + key, "request_conflict")
        seen.add(key)
    return prose.strip(), requests


class ReplyService:
    """Treat one pasted message as one exchange while retaining per-request deduplication."""

    def __init__(self, workflow):
        """Use durable workspace records and the same manual bridge as the CLI."""
        self.workflow = workflow
        self.ws = workflow.ws
        self.history = workflow.history
        self.prompts = PromptService(workflow)
        self.bridge = ManualBridge(self.ws, workflow.notify)

    def import_reply(self, session_id: str, text: str) -> dict:
        """Import once, validating the complete reply before executing its first operation."""
        self.workflow.check(session_id)
        reply_id = "reply-" + digest(text.strip())
        try:
            previous = self.ws.get("reply", session_id + ":" + reply_id)
        except PilotError as exc:
            if exc.code != "not_found":
                raise
        else:
            if previous["status"] == "processing":
                result = {"ok": False, "session_id": session_id, "reply_id": reply_id,
                          "error": {"code": "request_interrupted", "message": "Reply execution was interrupted. Inspect its request and command records; nothing was replayed."},
                          "next": "Inspect local changes/logs and use a new ID only for the next intentional action."}
                result["copy_text"] = self.prompts.copy_reply(session_id, dict(result))
                self.history.add(session_id, "harness", {**result, "text": result["error"]["message"]}, "interrupted:" + reply_id)
                return result
            return self.present(previous)
        try:
            prose, requests = parse_reply(text)
        except PilotError as exc:
            self.history.add(session_id, "model", {"text": text, "raw": text}, "model:" + reply_id)
            self.workflow.sessions.record(session_id, "model:" + reply_id, "model_reply", text)
            result = {"protocol_version": "1.0", "session_id": session_id, "reply_id": reply_id,
                      "ok": False, "error": {"code": exc.code, "message": str(exc)},
                      "next": "Correct the indicated field/fence and paste the corrected reply. No requests in this reply executed."}
            result["copy_text"] = self.prompts.copy_reply(session_id, dict(result))
            self.history.add(session_id, "harness", {**result, "text": str(exc)}, "invalid:" + reply_id)
            return result
        self.history.add(session_id, "model", {"text": prose or "Tool requests", "raw": text}, "model:" + reply_id)
        self.workflow.sessions.record(session_id, "model:" + reply_id, "model_reply", text)
        record = {"reply_id": reply_id, "session_id": session_id, "requests": requests,
                  "results": [], "next_index": 0, "status": "processing", "pending": None}
        return self.advance(record)

    def persist(self, record: dict) -> None:
        """Journal progress before/after each operation so reconnect never resumes it implicitly."""
        self.ws.put("reply", record["session_id"] + ":" + record["reply_id"], record)

    def advance(self, record: dict) -> dict:
        """Execute until completion, failure, or an explicitly local decision is needed."""
        session_id = record["session_id"]
        record.update(status="processing", pending=None)
        self.persist(record)
        for index in range(record["next_index"], len(record["requests"])):
            request = record["requests"][index]
            try:
                result = self.bridge.execute(session_id, canonical(request), record_message=False)
            except (PilotError, OSError) as exc:
                result = {"ok": False, "request_id": request["request_id"],
                          "error": {"code": getattr(exc, "code", "local_error"), "message": str(exc)}}
            record["results"].append(result)
            record["next_index"] = index + 1
            data = result.get("data", {})
            if not result.get("ok"):
                record["status"] = "failed"
                break
            if data.get("pending_permission"):
                record.update(status="awaiting_permission", pending={"kind": "command", "command_id": data["command_id"],
                                                                     "argv": data["argv"], "cwd": data["cwd"]})
                break
            if request["tool"] == "propose_patch":
                record.update(status="awaiting_approval", pending={"kind": "patch", "patch_id": data["patch_id"]})
                break
            if request["tool"] == "run_command" and (data.get("status") != "completed" or data.get("source_observation_incomplete")):
                record["status"] = "failed"
                break
            if request["tool"] == "validate" and data.get("validation", {}).get("status") != "passed":
                record["status"] = "failed"
                break
            self.persist(record)
        else:
            record["status"] = "completed"
        self.persist(record)
        self.workflow.refresh_after(session_id)
        return self.present(record)

    def present(self, record: dict) -> dict:
        """Build a copyable final reply or a local approval card without clipboard side effects."""
        result = {key: record[key] for key in ("reply_id", "session_id", "status", "pending", "results")}
        result.update(protocol_version="1.0", ok=record["status"] not in {"failed", "rejected"},
                      remaining_request_ids=[r["request_id"] for r in record["requests"][record["next_index"]:]])
        if record["pending"]:
            result["copy_text"] = ""
        elif not record["requests"]:
            result["copy_text"] = ""
            self.history.add(record["session_id"], "harness", {"text": "Reply received. No tool calls were requested. Type a message to prepare your next WebUI prompt."}, "ack:" + record["reply_id"])
        else:
            key = "reply-output:" + record["session_id"] + ":" + record["reply_id"]
            saved = self.history.preference(key)
            if saved:
                result["copy_text"] = saved
            else:
                result["copy_text"] = self.prompts.copy_reply(record["session_id"], dict(result))
                self.history.preference(key, result["copy_text"])
            self.history.add(record["session_id"], "harness", {"text": "Tool reply ready." if result["ok"] else "A tool needs correction. Copy the result back to the model.",
                            "copy_text": result["copy_text"], "reply_id": record["reply_id"], "status": record["status"]}, "reply-result:" + record["reply_id"])
        return result

    def resolve(self, session_id: str, reply_id: str, approved: bool, profile: str | None = None) -> dict:
        """Resolve a local approval once; model JSON has no access to this method."""
        self.workflow.check(session_id)
        record = self.ws.get("reply", session_id + ":" + reply_id)
        if record["status"] not in {"awaiting_permission", "awaiting_approval"} or not record["pending"]:
            raise PilotError("This reply is not awaiting local approval; inspect its recorded result", "approval_conflict")
        pending = record["pending"]
        # Reserve the local decision before any command/application can have effects.
        record["status"] = "processing"
        self.persist(record)
        request_id = record["requests"][record["next_index"] - 1]["request_id"]
        try:
            if pending["kind"] == "patch":
                result = self.workflow.approve(session_id, pending["patch_id"], approved, profile)
            elif approved:
                self.workflow.sessions.configure(session_id, commands_allowed=True)
                with self.ws.lock():
                    data = CommandRunner(self.ws, self.workflow.notify).execute(session_id, pending["command_id"])
                managed = self.bridge.tools.managed("run_command", record["requests"][record["next_index"] - 1]["arguments"], data,
                                                    "chat:" + session_id, success=data.get("status") == "completed")
                result = {"ok": True, "session_id": session_id, **managed}
            else:
                command = CommandRunner(self.ws).get(session_id, pending["command_id"])
                command["status"] = "rejected"
                self.ws.put("command", pending["command_id"], command)
                result = {"ok": False, "error": {"code": "permission_denied", "message": "The developer declined command execution."}}
        except (PilotError, OSError, ValueError) as exc:
            result = {"ok": False, "error": {"code": getattr(exc, "code", "local_error"), "message": str(exc)}}
        result["request_id"] = request_id
        record["results"][-1] = result
        record["pending"] = None
        if pending["kind"] == "command":
            with self.ws.db() as db:
                db.execute("UPDATE requests SET response=? WHERE session=? AND id=?", (canonical(result), session_id, request_id))
            self.bridge.save_response(session_id, request_id + ":decision", result)
        evidence = result.get("data", {}).get("validation", {}).get("status")
        if not approved or not result.get("ok") or result.get("data", {}).get("source_observation_incomplete") or (evidence and evidence not in {"passed", "not_run"}) or result.get("data", {}).get("status") in {"failed", "cancelled", "timed_out", "interrupted"}:
            record["status"] = "rejected" if not approved else "failed"
            self.persist(record)
            return self.present(record)
        return self.advance(record)
