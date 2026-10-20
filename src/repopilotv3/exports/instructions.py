"""Reusable project behavior, independently budgeted from session introductions."""

from importlib.resources import files

from repopilotv3.core import PilotError, digest
from repopilotv3.manual.sessions import measure


class ProjectInstructions:
    """Load the standalone project prompt without repository, task or session data."""

    MAX_CHARACTERS = 8000
    REVISION = 8

    def check_budget(self, text: str) -> None:
        """Reject oversized project instructions without discarding any operating rules."""
        if len(text) > self.MAX_CHARACTERS:
            raise PilotError(f"Project instructions exceed the {self.MAX_CHARACTERS:,}-character ceiling.", "prompt_budget")

    def render(self) -> str:
        """Return complete instructions or fail when their character budget is exceeded."""
        text = files("repopilotv3.resources").joinpath("webui.md").read_text("utf-8")
        self.check_budget(text)
        return text

    def describe(self) -> dict:
        """Describe copyable guidance without creating a chat or claiming a remote paste."""
        text = self.render()
        return {"text": text, "copy_text": text, "instruction_revision": self.REVISION,
                "fingerprint": digest(text), "max_characters": self.MAX_CHARACTERS, **measure(text)}
