"""UI-independent session workflow for CLI and the VS Code management connection."""

from __future__ import annotations

import json
import sqlite3

from repopilotv3.core import PilotError, atomic_json, canonical, digest, load_json, now, safe_path, uid
from repopilotv3.exports.manager import ExportManager
from repopilotv3.manual.bridge import ManualBridge
from repopilotv3.manual.patches import PatchManager
from repopilotv3.manual.sessions import SessionStore, deletion_path, session_operation
from repopilotv3.manual.validation import ValidationEvidence, validate
from repopilotv3.repository.index import RepositoryIndex, doctor
from repopilotv3.tasks.store import TaskStore
from repopilotv3.workflow.history import HistoryStore


class WorkflowService:
    """Bind every action explicitly to a workspace and session, with no clipboard dependency."""

    def __init__(self, ws, notify=None, defer_exports: bool = False):
        """Attach shared services; starting the service does not restore or execute a request."""
        self.ws = ws
        self.notify = notify
        self.defer_exports = defer_exports
        self.sessions = SessionStore(ws)
        self.history = HistoryStore(ws)
        for marker in list((ws.state / "deleted-sessions").glob("*.json"))[:10]:
            record = load_json(marker, {})
            if record.get("cleanup_pending"):
                try:
                    self.cleanup_deleted(record["id"])
                except (OSError, sqlite3.Error):
                    # The durable marker remains authoritative if private cleanup must wait.
                    pass

    def check(self, session_id: str) -> dict:
        """Validate a concrete session and reject new work in archived conversations."""
        session = self.sessions.get(session_id)
        if session.get("archived"):
            raise PilotError("Resume this archived session before continuing", "session_archived")
        return session

    def create(
        self,
        name: str = "New session",
        task_id: str | None = None,
        objective: str | None = None,
        project: str | None = None,
    ) -> dict:
        """Create an independent chat, optionally resuming an existing Copilot checkpoint."""
        project = project or self.history.preference("default_project") or "default"
        if not name.strip() or len(name) > 160:
            raise PilotError("Session names must contain 1–160 characters", "invalid_name")
        if not project.strip() or len(project) > 2000:
            raise PilotError("External project names must contain 1–2000 characters", "invalid_project")
        if task_id and objective:
            raise PilotError("Choose an existing task or a new objective", "invalid_arguments")
        if objective:
            with self.ws.lock():
                task_id = TaskStore(self.ws).create(objective)["id"]
        session = self.sessions.create(task_id, project)
        self.sessions.update(session["id"], name=name)
        self.select(session["id"])
        self.history.add(session["id"], "harness", {"text": "Session ready in Planning mode. Preparing your WebUI starter; no model is connected.", "action": "session_created", "task_id": task_id})
        return self.status(session["id"])

    def select(self, session_id: str) -> dict:
        """Persist the selected chat while leaving request history passive."""
        self.sessions.get(session_id)
        self.history.preference("selected_session", session_id)
        return {"session_id": session_id}

    def delete(self, session_id: str) -> dict:
        """Hide a chat immediately without waiting behind repository scans or exports."""
        marker = deletion_path(self.ws, session_id)
        if marker.exists():
            self.cleanup_deleted(session_id)
            return {"deleted": True, "session_id": session_id}
        with self.sessions.activity(session_id):
            session = self.sessions.get(session_id)
            from repopilotv3.manual.commands import CommandRunner

            with self.ws.db() as db:
                commands = [json.loads(row[0]) for row in db.execute(
                    "SELECT value FROM records WHERE kind='command' AND json_extract(value,'$.session_id')=? AND json_extract(value,'$.status')='running'", (session_id,))]
                patches = [json.loads(row[0]) for row in db.execute(
                    "SELECT value FROM records WHERE kind='patch' AND json_extract(value,'$.session_id')=?", (session_id,))]
            for command in commands:
                if CommandRunner(self.ws).get(session_id, command["command_id"])["status"] == "running":
                    raise PilotError("Stop the running command before deleting this session.", "session_running")
            for patch in patches:
                journal = load_json(self.ws.state / "patches" / patch["id"] / "recovery" / "journal.json", {})
                if journal.get("state") in {"prepared", "recovery_required"}:
                    raise PilotError("Recover the interrupted patch before deleting this session.", "recovery_required")
            # This small durable marker is independent of source-index SQLite writes.
            atomic_json(marker, {"id": session_id, "task_id": session["task_id"],
                                 "deleted_at": now(), "cleanup_pending": True})
            warning = None
            try:
                cleaned = self.cleanup_deleted(session_id)
            except (OSError, sqlite3.Error) as exc:
                cleaned, warning = False, str(exc)
            return {"deleted": True, "session_id": session_id, "task_retained": session["task_id"],
                    "cleanup_pending": not cleaned,
                    "cleanup_warning": warning,
                    "note": "Shared tasks, validation/command evidence, immutable exports and recovery files are retained."}

    def cleanup_deleted(self, session_id: str) -> bool:
        """Purge deleted chat records briefly; a busy index cannot undo or delay the tombstone."""
        marker = deletion_path(self.ws, session_id)
        record = load_json(marker)
        if not record:
            return False
        self.history.purge(session_id)
        try:
            with self.ws.db(timeout=0.1) as db:
                db.execute("INSERT OR REPLACE INTO records VALUES('deleted_session',?,?)", (session_id, canonical(record)))
                db.execute("DELETE FROM records WHERE kind='session' AND id=?", (session_id,))
                db.execute("DELETE FROM records WHERE kind='run' AND id=?", ("chat:" + session_id,))
                db.execute("DELETE FROM records WHERE kind='reply' AND substr(id,1,?)=?", (len(session_id) + 1, session_id + ":"))
                db.execute("DELETE FROM requests WHERE session=?", (session_id,))
                db.execute("DELETE FROM events WHERE session=?", (session_id,))
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc).lower() and "busy" not in str(exc).lower():
                raise
            return False
        for name in ("current-", "signature-"):
            (self.ws.state / "exports" / f"{name}{session_id}.json").unlink(missing_ok=True)
        record["cleanup_pending"] = False
        atomic_json(marker, record)
        return True

    def status(self, session_id: str) -> dict:
        """Read lightweight local state even while a build or index job is running."""
        session = self.sessions.get(session_id)
        task = TaskStore(self.ws).get(session["task_id"]) if session["task_id"] else None
        manifest = load_json(self.ws.state / "exports" / f"current-{session_id}.json")
        checklist = ExportManager(self.ws).checklist(manifest) if manifest else []
        with self.ws.db() as db:
            pending = [
                r[0]
                for r in db.execute(
                    "SELECT id FROM requests WHERE session=? AND response IS NULL", (session_id,)
                )
            ]
            waiting = [
                {"reply_id": r[0], "pending": json.loads(r[1]), "status": r[2]}
                for r in db.execute("SELECT json_extract(value,'$.reply_id'), json_extract(value,'$.pending'), json_extract(value,'$.status') FROM records WHERE kind='reply' AND id LIKE ? AND json_extract(value,'$.status') IN ('awaiting_permission','awaiting_approval')",
                                    (session_id + ":%",))
            ]
            commands = [json.loads(r[0]) for r in db.execute(
                "SELECT value FROM records WHERE kind='command' AND json_extract(value,'$.session_id')=? AND json_extract(value,'$.status')='running'", (session_id,))]
        selected = self.history.preference("selected_session")
        stale = True
        if manifest and selected == session_id:
            try:
                stale = (manifest["snapshot"] != RepositoryIndex(self.ws).current_id()
                         or bool(self.history.preference("package-dirty:" + session_id)))
            except PilotError:
                pass
        return {
            "session": session,
            "task": task,
            "draft": self.history.preference("draft:" + session_id) or "",
            "metrics": self.sessions.metrics(session_id),
            "package_stale": stale,
            "checklist": checklist,
            "directory": manifest["directory"] if manifest else None,
            "generation_id": manifest["id"] if manifest else None,
            "interrupted_requests": pending,
            "profiles": list(self.ws.config["profiles"]),
            "default_validation": self.ws.config["default_validation"],
            "pending_replies": waiting,
            "running_commands": [{"command_id": c["command_id"], "argv": c["argv"]} for c in commands],
        }

    def starter(self, session_id: str) -> dict:
        """Generate the combined copy-ready kickoff only after an explicit foreground action."""
        from repopilotv3.workflow.prompts import PromptService

        return PromptService(self).starter(session_id)

    @session_operation
    def configure(self, session_id: str, **settings) -> dict:
        """Apply local mode, permission and context controls outside the model registry."""
        from repopilotv3.workflow.prompts import PromptService

        return PromptService(self).configure(session_id, **settings)

    @session_operation
    def compose(self, session_id: str, text: str) -> dict:
        """Record a developer message and return the prompt to paste into the WebUI."""
        from repopilotv3.workflow.prompts import PromptService

        return PromptService(self).compose(session_id, text)

    @session_operation
    def import_reply(self, session_id: str, text: str) -> dict:
        """Handle prose and prevalidated tools as one durable incoming conversation message."""
        from repopilotv3.workflow.replies import ReplyService

        return ReplyService(self).import_reply(session_id, text)

    @session_operation
    def resolve_reply(self, session_id: str, reply_id: str, approved: bool, profile: str | None = None) -> dict:
        """Resolve local permission/application once and continue only unexecuted queued requests."""
        from repopilotv3.workflow.replies import ReplyService

        return ReplyService(self).resolve(session_id, reply_id, approved, profile)

    def reply_status(self, session_id: str, reply_id: str) -> dict:
        """Inspect a pending reply without resuming tools or emitting copyable messages."""
        self.sessions.get(session_id)
        record = self.ws.get("reply", session_id + ":" + reply_id)
        return {"reply_id": reply_id, "pending": record["pending"], "status": record["status"],
                "has_commands": any(r["tool"] == "run_command" for r in record["requests"])}

    def prepare(self, session_id: str, full_source: bool | None = None, lock_timeout: float = 30) -> dict:
        """Refresh source and atomically publish changed context for one explicit chat."""
        session = self.check(session_id)
        preference = "full_source:" + session_id
        if full_source is not None:
            self.history.preference(preference, full_source)
        full = bool(self.history.preference(preference))
        with self.ws.lock(timeout=lock_timeout):
            session = self.check(session_id)
            snap = RepositoryIndex(self.ws).refresh()
            task = TaskStore(self.ws).get(session["task_id"]) if session["task_id"] else None
            command_evidence = ExportManager(self.ws).command_evidence(task["id"] if task else None, snap["id"])
            signature = digest(canonical([snap["id"], task, session, full, self.ws.config, command_evidence,
                                          ExportManager(self.ws).template_fingerprint()]))
            manifest = load_json(self.ws.state / "exports" / f"current-{session_id}.json")
            signature_path = self.ws.state / "exports" / f"signature-{session_id}.json"
            saved = load_json(signature_path, {})
            if (
                manifest
                and signature == saved.get("signature")
                and manifest["id"] == saved.get("generation_id")
            ):
                self.history.preference("package-dirty:" + session_id, False)
                from repopilotv3.exports.cleanup import ExportCleanup

                return {
                    "manifest": manifest,
                    "directory": manifest["directory"],
                    "checklist": ExportManager(self.ws).checklist(manifest),
                    "unchanged": True,
                    "cleanup": ExportCleanup(self.ws).run(),
                }
            result = ExportManager(self.ws).prepare(session_id, full)
            atomic_json(signature_path, {"signature": signature, "generation_id": result["manifest"]["id"]})
            self.history.preference("package-dirty:" + session_id, False)
            return result

    def refresh_after(self, session_id: str) -> None:
        """Defer panel package work until after its reply; CLI callers retain synchronous refresh."""
        if self.defer_exports:
            self.history.preference("package-dirty:" + session_id, True)
            return
        try:
            self.prepare(session_id)
        except (PilotError, OSError) as exc:
            self.history.add(session_id, "local", {"action": "package_refresh_failed", "error": str(exc)})

    @session_operation
    def execute(self, session_id: str, text: str) -> dict:
        """Dispatch one manual message once; export failure never disguises completed effects."""
        self.check(session_id)
        try:
            result = ManualBridge(self.ws, self.notify).execute(session_id, text)
        except PilotError as exc:
            result = {
                "protocol_version": "1.0",
                "ok": False,
                "session_id": session_id,
                "error": {"code": exc.code, "message": str(exc)},
            }
            self.history.add(session_id, "result", result)
        self.refresh_after(session_id)
        return result

    @session_operation
    def note(self, session_id: str, text: str) -> dict:
        """Save local developer prose without sending it to a model or invoking tools."""
        self.check(session_id)
        entry = self.history.add(session_id, "note", {"text": text, "origin": "developer"})
        return {"entry_id": entry, "saved": True}

    @session_operation
    def progress(self, session_id: str, progress: dict) -> dict:
        """Merge developer-authored progress without granting model TODO readiness or evidence."""
        from repopilotv3.manual.schema import PROGRESS, validate as validate_schema

        session = self.check(session_id)
        if not session["task_id"]:
            raise PilotError("Initialize or select a task first", "task_required")
        validate_schema(progress, PROGRESS)
        with self.ws.lock():
            task = TaskStore(self.ws).update(session["task_id"], progress, origin="developer")
        self.history.add(session_id, "local", {"action": "progress", "progress": progress, "verified": False})
        return task

    def patch(self, session_id: str, patch_id: str) -> dict:
        """Authorize a proposal only for the session that staged it."""
        self.check(session_id)
        patch = self.ws.get("patch", patch_id)
        if patch["session_id"] != session_id:
            raise PilotError("Patch belongs to a different session", "wrong_session")
        return patch

    def review(
        self, session_id: str, patch_id: str, path: str | None = None, metadata_only: bool = False
    ) -> dict:
        """Return complete immutable original/proposed text for native read-only diffs."""
        patch = self.patch(session_id, patch_id)
        with self.ws.lock():
            manager = PatchManager(self.ws)
            result = manager.review(patch_id)
            outputs = manager.validate(patch["proposal"])
            files = []
            if path is not None and path not in outputs:
                raise PilotError("File is not part of this proposal", "not_found")
            for relative, proposed in outputs.items():
                if path is not None and path != relative:
                    continue
                source = safe_path(self.ws.root, relative)
                original = source.read_bytes() if source.exists() else None
                files.append(
                    {
                        "path": relative,
                        **(
                            {}
                            if metadata_only
                            else {
                                "original": original.decode("utf-8") if original is not None else "",
                                "proposed": proposed.decode("utf-8") if proposed is not None else "",
                            }
                        ),
                        "status": "added"
                        if original is None
                        else "deleted"
                        if proposed is None
                        else "modified",
                    }
                )
        return {**result, "files": files, "session_id": session_id, "whole_proposal_only": True}

    @session_operation
    def approve(self, session_id: str, patch_id: str, approved: bool, profile: str | None = None) -> dict:
        """Apply or reject the whole reviewed proposal and record actual validation afterward."""
        patch = self.patch(session_id, patch_id)
        profile = profile or self.ws.config["default_validation"]
        if profile is not None and profile not in self.ws.config["profiles"]:
            raise PilotError("Select a configured validation profile before applying", "unknown_profile")
        action_id = uid("approval")
        self.history.add(
            session_id,
            "local",
            {"action": "patch_approval_started", "patch_id": patch_id, "approved": approved},
            action_id,
        )
        with self.ws.lock():
            if approved:
                result = PatchManager(self.ws).apply(patch_id, True)
                if profile:
                    try:
                        record = validate(self.ws, profile, patch["task_id"])
                        result["validation"] = ValidationEvidence(self.ws).inspect(
                            record["snapshot"], record["id"]
                        )
                    except (PilotError, OSError) as exc:
                        result["validation"] = {"status": "not_completed", "error": str(exc)}
                else:
                    result["validation"] = {"status": "not_run", "reason": "No validation profile selected."}
            else:
                if patch["status"] != "staged":
                    raise PilotError("Only a staged patch can be rejected", "conflict")
                patch["status"] = "rejected"
                self.ws.put("patch", patch_id, patch)
                result = {"patch_id": patch_id, "status": "rejected", "source_changed": False}
        response = {
            "protocol_version": "1.0",
            "ok": True,
            "session_id": session_id,
            "task_id": patch["task_id"],
            "local_action": "patch_review",
            "data": result,
        }
        ManualBridge(self.ws).save_response(session_id, action_id, response)
        self.refresh_after(session_id)
        return response

    @session_operation
    def recover(self, session_id: str, patch_id: str) -> dict:
        """Explicitly recover an interrupted application without replaying its validation."""
        self.patch(session_id, patch_id)
        with self.ws.lock():
            result = PatchManager(self.ws).recover(patch_id)
        self.history.add(session_id, "local", {"action": "patch_recovery", **result})
        return result

    @session_operation
    def confirm(self, session_id: str, generation_id: str, confirmations: dict) -> list[dict]:
        """Record actual user-confirmed transfers for exact artifact versions in this scope."""
        self.check(session_id)
        with self.ws.lock():
            return ExportManager(self.ws).confirm(generation_id, session_id, confirmations)

    @session_operation
    def transfer(self, session_id: str, text: str) -> dict:
        """Account for an explicit local copy exactly once; remote paste remains unknown."""
        self.sessions.get(session_id)
        self.sessions.record(session_id, "transfer:" + digest(text), "outgoing", text)
        return {"recorded": True, "remote_paste_confirmed": False}

    @session_operation
    def handoff(self, session_id: str, confirmed_new_chat: bool = False) -> dict:
        """Create a replacement binding only after the developer confirms a new external chat."""
        session = self.check(session_id)
        if not session["task_id"]:
            raise PilotError("Initialize a task before handoff", "task_required")
        if not confirmed_new_chat:
            prepared = self.prepare(session_id)
            return {"requires_new_chat_confirmation": True, **prepared}
        new = self.sessions.create(session["task_id"], session["project"])
        self.sessions.configure(new["id"], mode=session["mode"], context_limit=session["context_limit"])
        self.sessions.update(new["id"], name=session.get("name", "Conversation")[:148] + " (continued)")
        self.history.add(
            new["id"],
            "local",
            {"action": "continued", "previous_session": session_id, "task_id": session["task_id"]},
        )
        self.select(new["id"])
        prepared = self.prepare(new["id"])
        starter = self.starter(new["id"])
        return {"session_id": new["id"], "previous_session": session_id, **prepared,
                "copy_text": starter["copy_text"]}

    def diagnostics(self) -> dict:
        """Describe backend capabilities without pretending to inspect Copilot's runtime."""
        from repopilotv3 import __version__
        from repopilotv3.integrations.monitor import Monitor

        return {
            "backend_version": __version__,
            "bridge_protocol": "1.0",
            "workspace": str(self.ws.root),
            "state": str(self.ws.state),
            "panel_minimum_vscode": "1.85.0",
            "model_connections": False,
            "harness_managed": False,
            "export_storage": {"policy": self.ws.config["export_cleanup"],
                               "last_cleanup": load_json(self.ws.state / "exports/cleanup.json", {})},
            "environment": doctor(self.ws),
            "monitoring": Monitor(self.ws).status(),
            "hooks": load_json(
                self.ws.state / "hook-health.json",
                {"coverage": "unknown", "reason": "No compatible hook events observed"},
            ),
            "copilot_capabilities": "Not probed; verify MCP/agents/hooks in the host editor separately.",
        }
