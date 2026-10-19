"""Copy-ready local harness messages, advisory modes and context continuation guidance."""

from __future__ import annotations

from pathlib import Path

from repopilotv3.core import canonical, digest, uid
from repopilotv3.manual.schema import ARGUMENTS

MODES = {
    "plan": ("Planning", "Clarify the objective and constraints. Explore bounded context, compare material tradeoffs, and maintain an actionable TODO with acceptance criteria. Ask focused questions before uncertain implementation."),
    "ask": ("Asking", "Answer the developer's question directly. Cite current source paths, distinguish evidence from inference and retrieve only missing context. Keep the task/TODO current when the objective changes."),
    "code": ("Implementation", "Implement the next TODO item with focused reviewed patches. Run targeted checks, inspect actual evidence, correct failures and checkpoint progress. Avoid broad rewrites and repeated unchanged retrieval."),
    "review": ("Review", "Inspect changes for concrete defects, regressions and missing evidence. Cite paths and explain impact. Run relevant checks when authorized; distinguish confirmed findings from questions and model assumptions."),
}


class PromptService:
    """Render useful harness replies without making a model call or replaying an operation."""

    def __init__(self, workflow):
        """Reuse the selected workspace's durable history, session and export services."""
        self.workflow = workflow
        self.ws = workflow.ws
        self.history = workflow.history
        self.sessions = workflow.sessions

    def save(self, session_id: str, text: str, entry_id: str | None = None, **extra) -> dict:
        """Record one copyable local message independently of clipboard availability."""
        entry = self.history.add(session_id, "harness", {"text": text, "copy_text": text, **extra}, entry_id)
        return {"session_id": session_id, "entry_id": entry, "text": text, "copy_text": text, **extra}

    def catalog(self) -> str:
        """Describe actual registered arguments so a fresh model need not guess or upload a schema first."""
        lines = ["## Tool arguments", "", "All argument objects are closed. ? marks optional fields."]
        for tool, schema in ARGUMENTS.items():
            arguments = []
            for name, definition in schema["properties"].items():
                marker = "" if name in schema["required"] else "?"
                arguments.append(name + marker + ":" + str(definition.get("type", "object")))
            lines.append(f"- {tool}({', '.join(arguments)})")
        lines += ["", "progress: items[{id,text,status}], plus errors/files/decisions/questions/assumptions/required_tests as string arrays. Status is pending, active, blocked or completed.",
                  "base_hashes maps each affected relative path to its current hash (null for additions). diff is an exact unified diff. batch.calls uses [{tool,arguments}] for search/read/context/changes/command_output only. Use 07_PROTOCOL_REFERENCE.md for expanded examples."]
        return "\n".join(lines)

    def starter(self, session_id: str) -> dict:
        """Introduce this conversation separately from reusable project behavior and protocol."""
        session = self.workflow.check(session_id)
        saved = self.history.preference("starter:" + session_id)
        title, _ = MODES[session["mode"]]
        if (not session["task_id"] and isinstance(saved, dict) and saved.get("starter_version") == 2
                and saved.get("mode") == session["mode"] and saved.get("kind") == "new"):
            return saved
        kind = "continuation" if session["task_id"] else "new"
        parts = ["# " + ("Continue project chat" if session["task_id"] else "New project chat"),
                 f"Project: `{self.ws.root.name}`\nLocal session: `{session_id}`\nMode: {title}",
                 "Use this WebUI project's separately configured project instructions. "
                 "If they are unavailable, ask me to paste the project prompt first."]
        if session["task_id"]:
            package = self.workflow.prepare(session_id)
            continuation = (Path(package["directory"]) / "06_CONTINUATION.md").read_text("utf-8")
            parts += [continuation, "Resume this saved task. Initialize todo for this chat, then continue with its next unresolved step."]
        else:
            parts += ["This is a fresh conversation with no task or objective selected. "
                      "Ask what I want to work on if I have not supplied a goal. "
                      "Once it is clear, use task with objective and chat_name, then todo to register the checklist. "
                      "Do not invent an objective."]
        text = "\n\n".join(parts)
        if isinstance(saved, dict) and saved.get("starter_version") == 2 and saved.get("text") == text:
            return saved
        entry_id = "starter:v2:" + session_id + ":" + digest(text)
        result = self.save(session_id, text, entry_id, action="starter", starter_version=2,
                           mode=session["mode"], kind=kind)
        self.history.preference("starter:" + session_id, result)
        return result

    def compose(self, session_id: str, text: str) -> dict:
        """Prepare typed developer text for the external model without interpreting it as commands."""
        session = self.workflow.check(session_id)
        message = uid("user")
        self.history.add(session_id, "user", {"text": text}, message)
        task = session["task_id"] or "not initialized"
        forwarded = f"Session: {session_id} · Task: {task} · Mode: {MODES[session['mode']][0]}\n\n{text}\n\nRespond concisely; use RepoPilot JSON requests for local tools and keep the TODO current."
        return self.save(session_id, forwarded, "forward:" + message, action="forward")

    def configure(self, session_id: str, **settings) -> dict:
        """Persist local controls and emit a short mode delta only when the mode changes."""
        previous = self.workflow.check(session_id)
        session = self.sessions.configure(session_id, **settings)
        if settings.get("project"):
            self.history.preference("default_project", settings["project"])
        self.history.add(session_id, "local", {"action": "session_settings", **settings})
        if previous["mode"] != session["mode"]:
            title, guidance = MODES[session["mode"]]
            return self.save(session_id, f"Mode changed to {title}.\nSession: {session_id}\n\n{guidance}\n\nRetain the existing task and TODO. Use the same tool protocol; no operation has executed because the mode changed.", action="mode")
        return {"session_id": session_id, "session": session}

    def advise(self, session_id: str, result: dict) -> dict:
        """Append one context-threshold notice to a foreground tool reply and prepare its handoff."""
        metrics = self.sessions.metrics(session_id)
        key = f"context-advised:{session_id}:{metrics['context_limit']}"
        if metrics["handoff_recommended"] and not self.history.preference(key):
            notice = f"Observed context has reached ~{metrics['observed_estimated_tokens']} estimated tokens against the {metrics['context_limit']} workflow threshold. Update TODO and consider a fresh chat; this is advisory, not measured remote capacity."
            try:
                package = self.workflow.prepare(session_id)
                result.setdefault("advisories", []).append({"message": notice, "directory": package["directory"],
                                                           "next": "Use Generate Handoff to continue in a confirmed new external chat."})
            except Exception as exc:
                result.setdefault("advisories", []).append({"message": notice, "package_error": str(exc)})
            self.history.preference(key, True)
            self.save(session_id, notice, "advisory:" + digest(key), action="context_advisory")
        return result

    def copy_reply(self, session_id: str, result: dict) -> str:
        """Preserve all already-bounded tool results when combining a complete turn for transfer."""
        from repopilotv3.workflow.formatting import ReplyFormatter

        return canonical(ReplyFormatter().compact(self.advise(session_id, result)))
