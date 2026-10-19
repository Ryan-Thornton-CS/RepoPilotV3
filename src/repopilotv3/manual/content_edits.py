"""Generate exact reviewable unified diffs from complete proposed UTF-8 file contents."""

from __future__ import annotations

import difflib

from repopilotv3.core import PilotError


class ContentDiff:
    """Handle hunk counts and final-newline markers locally rather than asking a model."""

    @staticmethod
    def generate(path: str, original: bytes | None, content: str | None) -> str:
        """Return a normalized logical diff, preserving the base's encoding/newline conventions on apply."""
        if any(char in path for char in "\t\r\n"):
            raise PilotError("File-content proposal paths cannot contain tabs or newlines", "unsafe_path")
        if original is None and content is None:
            raise PilotError(f"Cannot delete a missing file: {path}", "invalid_patch")
        try:
            before = original.decode("utf-8-sig") if original is not None else ""
            if "\x00" in before or "\r" in before.replace("\r\n", ""):
                raise PilotError(f"Not supported UTF-8/LF/CRLF source: {path}", "invalid_patch")
            if "\r\n" in before and "\n" in before.replace("\r\n", ""):
                raise PilotError(f"Mixed source line endings; normalize explicitly first: {path}", "invalid_patch")
            after = content if content is not None else ""
            if original is not None and original.startswith(b"\xef\xbb\xbf") and after.startswith("\ufeff"):
                after = after[1:]
            if "\x00" in after or "\r" in after.replace("\r\n", ""):
                raise PilotError(f"Proposed content must be UTF-8 text with LF or CRLF: {path}", "invalid_patch")
            after.encode("utf-8")
        except UnicodeError as exc:
            raise PilotError(f"File-content proposals require valid UTF-8 text: {path}", "invalid_patch") from exc
        before, after = before.replace("\r\n", "\n"), after.replace("\r\n", "\n")
        # Logical lines use LF in the diff. Existing source conventions are restored by transform().
        if original is not None and content is not None and before == after:
            return ""
        old_name = "/dev/null" if original is None else "a/" + path
        new_name = "/dev/null" if content is None else "b/" + path
        lines = list(difflib.unified_diff(before.splitlines(True), after.splitlines(True), old_name, new_name))
        if not lines:
            # Empty additions/deletions still change existence and must remain reviewable.
            return f"--- {old_name}\n+++ {new_name}\n@@ -0,0 +0,0 @@\n"
        return "".join(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n" for line in lines)
