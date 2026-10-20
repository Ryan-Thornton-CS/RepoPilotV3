"""Project-owned role prompts with explicit, recoverable resets and packaged defaults."""

from __future__ import annotations

from importlib.resources import files

from repopilotv3.core import PilotError, atomic_bytes, atomic_json, digest, now, uid

ROLES = {"planning": "Planning", "asking": "Asking", "implementation": "Implementation", "review": "Review"}


class RolePrompts:
    """Read editable Markdown as the authority; upgrades never replace authored prompts."""

    def __init__(self, ws):
        """Keep prompt files and recovery records inside this project's private state."""
        self.ws = ws
        self.directory = ws.state / "prompts"

    def path(self, role: str):
        """Reject unknown roles and symlink targets before any prompt read or write."""
        if role not in ROLES:
            raise PilotError("Choose planning, asking, implementation or review", "invalid_role")
        target = self.directory / (role + ".md")
        if self.directory.is_symlink() or target.is_symlink():
            raise PilotError("Prompt files must not be symlinks", "unsafe_path")
        return target

    def default(self, role: str) -> str:
        """Load versioned packaged guidance independently of the static Copilot agents."""
        self.path(role)
        return files("repopilotv3.resources").joinpath("prompts", role + ".md").read_text("utf-8")

    def read(self, role: str) -> dict:
        """Initialize a missing private prompt, then return its exact authored contents."""
        with self.ws.lock():
            target = self.path(role)
            default = self.default(role)
            if not target.exists():
                atomic_bytes(target, default.encode("utf-8"))
            try:
                data = target.read_bytes()
                text = data.decode("utf-8")
            except UnicodeError as exc:
                raise PilotError("Save the prompt as UTF-8 Markdown", "invalid_prompt") from exc
            return {"role": role, "title": ROLES[role], "path": str(target), "text": text,
                    "hash": digest(data), "customized": text != default, "default_version": 1}

    def reset(self, role: str, expected_hash: str) -> dict:
        """Back up the exact authored bytes and reset only the version the user reviewed."""
        with self.ws.lock():
            target = self.path(role)
            previous = target.read_bytes() if target.exists() else b""
            if digest(previous) != expected_hash:
                raise PilotError("Prompt changed while confirming. Review it and retry reset.", "stale_prompt")
            recovery = self.ws.state / "prompt-recovery" / uid("reset")
            atomic_bytes(recovery / (role + ".md"), previous)
            atomic_json(recovery / "record.json", {"role": role, "path": str(target), "at": now(),
                                                    "hash": expected_hash})
            text = self.default(role)
            atomic_bytes(target, text.encode("utf-8"))
            return {"role": role, "path": str(target), "text": text, "hash": digest(text.encode()),
                    "recovery": str(recovery), "customized": False}
