"""Exact UTF-8 unified patches, immutable proposals, and rollback-backed application."""

from __future__ import annotations

import os
import re

from repopilotv3.core import (
    PilotError,
    Workspace,
    atomic_bytes,
    atomic_json,
    digest,
    now,
    safe_path,
    uid,
)
from repopilotv3.repository.index import RepositoryIndex, allowed
from repopilotv3.manual.sessions import SessionStore
from repopilotv3.tasks.store import TaskStore
from repopilotv3.manual.content_edits import ContentDiff
from repopilotv3.manual.schema import ARGUMENTS, validate as validate_schema

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@(?:.*)$")


def parse_patch(text: str) -> list[dict]:
    """Parse bounded unified UTF-8 patches and reject unsupported or inconsistent structure."""
    if len(text.encode("utf-8")) > 2_000_000 or "\x00" in text:
        raise PilotError("Patch exceeds 2 MB or contains binary data", "invalid_patch")
    lines = text.splitlines()
    files, i = [], 0
    while i < len(lines):
        if not lines[i].startswith("--- "):
            # Accept conventional git preamble, but no rename/mode/binary semantics.
            if lines[i].startswith(("diff --git ", "index ")) or not lines[i].strip():
                i += 1
                continue
            raise PilotError(f"Unsupported patch line: {lines[i][:100]}", "invalid_patch")
        old = lines[i][4:].split("\t", 1)[0]
        i += 1
        if i == len(lines) or not lines[i].startswith("+++ "):
            raise PilotError("Missing +++ header", "invalid_patch")
        new = lines[i][4:].split("\t", 1)[0]
        i += 1
        if old != "/dev/null" and not old.startswith("a/") or new != "/dev/null" and not new.startswith("b/"):
            raise PilotError("Patch headers require a/ and b/ paths", "invalid_patch")
        old_path = old[2:] if old != "/dev/null" else None
        new_path = new[2:] if new != "/dev/null" else None
        if not old_path and not new_path or old_path and new_path and old_path != new_path:
            raise PilotError("Renames and empty paths are unsupported", "invalid_patch")
        entry = {
            "path": new_path or old_path,
            "addition": old_path is None,
            "deletion": new_path is None,
            "hunks": [],
        }
        while i < len(lines) and lines[i].startswith("@@ "):
            match = HUNK.match(lines[i])
            if not match:
                raise PilotError("Malformed hunk header", "invalid_patch")
            a, ac, b, bc = match.groups()
            hunk = {
                "old_start": int(a),
                "old_count": int(ac or 1),
                "new_start": int(b),
                "new_count": int(bc or 1),
                "lines": [],
            }
            i += 1
            consumed_old = consumed_new = 0
            while i < len(lines):
                line = lines[i]
                if line == "\\ No newline at end of file":
                    if not hunk["lines"]:
                        raise PilotError("Orphan newline marker", "invalid_patch")
                    hunk["lines"][-1]["newline"] = False
                    i += 1
                    continue
                if consumed_old == hunk["old_count"] and consumed_new == hunk["new_count"]:
                    break
                if not line or line[0] not in " +-":
                    raise PilotError("Unexpected hunk content", "invalid_patch")
                hunk["lines"].append({"kind": line[0], "text": line[1:], "newline": True})
                consumed_old += int(line[0] in " -")
                consumed_new += int(line[0] in " +")
                if consumed_old > hunk["old_count"] or consumed_new > hunk["new_count"]:
                    raise PilotError(f"Hunk counts do not match in {entry['path']}. Correct the diff or submit complete contents using files instead.", "invalid_patch")
                i += 1
            if (consumed_old, consumed_new) != (hunk["old_count"], hunk["new_count"]):
                raise PilotError(f"Incomplete hunk in {entry['path']}. Correct the diff or submit complete contents using files instead.", "invalid_patch")
            entry["hunks"].append(hunk)
        if not entry["hunks"]:
            raise PilotError("A file patch must have at least one hunk", "invalid_patch")
        files.append(entry)
    names = [f["path"] for f in files]
    if not files or len(files) > 100 or len(names) != len(set(names)):
        raise PilotError("Patch must contain 1–100 distinct files", "invalid_patch")
    return files


def transform(entry: dict, original: bytes | None) -> bytes | None:
    """Apply exact logical hunks in memory while preserving source newline conventions."""
    if entry["addition"] != (original is None):
        raise PilotError("Patch addition/existing-file mismatch", "invalid_patch")
    raw = original or b""
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")
    if "\x00" in text:
        raise PilotError("Binary patch target", "invalid_patch")
    newline = "\r\n" if "\r\n" in text else "\n"
    if "\r" in text.replace("\r\n", "") or "\r\n" in text and "\n" in text.replace("\r\n", ""):
        raise PilotError(
            "Mixed or unsupported line endings; normalize explicitly before patching", "invalid_patch"
        )
    source = text.splitlines(keepends=True)
    result, cursor = [], 0
    for hunk in entry["hunks"]:
        start = hunk["old_start"] if hunk["old_count"] == 0 else hunk["old_start"] - 1
        if start < cursor or start > len(source):
            raise PilotError("Overlapping or out-of-range hunk", "invalid_patch")
        result.extend(source[cursor:start])
        cursor = start
        new_start = hunk["new_start"] if hunk["new_count"] == 0 else hunk["new_start"] - 1
        if new_start != len(result):
            raise PilotError("New hunk coordinates do not match output", "invalid_patch")
        for line in hunk["lines"]:
            value = line["text"] + (newline if line["newline"] else "")
            if line["kind"] in " -":
                if cursor >= len(source) or source[cursor] != value:
                    raise PilotError("Patch context differs from exact base source", "stale_patch")
                cursor += 1
            if line["kind"] in " +":
                result.append(value)
    result.extend(source[cursor:])
    if entry["deletion"]:
        if result:
            raise PilotError("Deletion does not remove the complete file", "invalid_patch")
        return None
    return (b"\xef\xbb\xbf" if bom else b"") + "".join(result).encode("utf-8")


class PatchManager:
    """Stage reviewed exact patches and apply them with durable recovery backups."""

    def __init__(self, ws: Workspace):
        """Attach patch records and recovery journals to one workspace."""
        self.ws = ws

    def original(self, relative: str, expected: str | None) -> bytes | None:
        """Read an allowed regular source file only after checking its exact base hash."""
        from repopilotv3.core import git

        path = safe_path(self.ws.root, relative)
        if not allowed(self.ws, relative) or git(self.ws.root, "check-ignore", "--no-index", "--", relative).strip():
            raise PilotError(f"Patch target is excluded or ignored: {relative}", "unsafe_path")
        if path.exists() and not path.is_file():
            raise PilotError("Patch target is not a regular file", "unsafe_path")
        original = path.read_bytes() if path.exists() else None
        if (digest(original) if original is not None else None) != expected:
            raise PilotError(f"Patch base hash differs from source: {relative}", "stale_patch")
        return original

    def normalize(self, proposal: dict) -> dict:
        """Convert full-file proposals to immutable exact diffs without changing repository files."""
        validate_schema(proposal, ARGUMENTS["propose_patch"])
        if "diff" in proposal:
            return proposal
        snapshot = RepositoryIndex(self.ws).refresh()
        if proposal["base_snapshot"] != snapshot["id"]:
            raise PilotError("Patch base snapshot is stale; retrieve current context", "stale_patch")
        if set(proposal["base_hashes"]) != set(proposal["files"]):
            raise PilotError("Base hashes must cover exactly the paths in files", "invalid_patch")
        parts, hashes, targets = [], {}, set()
        total = 0
        for relative, content in proposal["files"].items():
            path = safe_path(self.ws.root, relative)
            target = os.path.normcase(str(path))
            if target in targets:
                raise PilotError(f"Duplicate target path: {relative}", "unsafe_path")
            targets.add(target)
            original = self.original(relative, proposal["base_hashes"][relative])
            diff = ContentDiff.generate(relative, original, content)
            total += len(diff.encode("utf-8"))
            if total > 2_000_000:
                raise PilotError("Generated patch exceeds 2 MB; split the proposal", "invalid_patch")
            if diff:
                parts.append(diff)
                hashes[relative] = proposal["base_hashes"][relative]
        if not parts:
            raise PilotError("Proposed files contain no changes; continue the task without a patch", "no_changes")
        return {"base_snapshot": proposal["base_snapshot"], "base_hashes": hashes, "diff": "".join(parts)}

    def validate(self, proposal: dict) -> dict[str, bytes | None]:
        """Recheck source snapshot, file hashes, paths, and all hunks before any edit."""
        proposal = self.normalize(proposal)
        snap = RepositoryIndex(self.ws).refresh()
        if proposal["base_snapshot"] != snap["id"]:
            raise PilotError("Patch base snapshot is stale; request a new proposal", "stale_patch")
        entries = parse_patch(proposal["diff"])
        if set(proposal["base_hashes"]) != {e["path"] for e in entries}:
            raise PilotError("Base hashes must cover exactly the affected paths", "invalid_patch")
        output = {}
        for entry in entries:
            original = self.original(entry["path"], proposal["base_hashes"][entry["path"]])
            output[entry["path"]] = transform(entry, original)
        return output

    def stage(
        self, proposal: dict, session_id: str, task_id: str, override_reason: str | None = None
    ) -> dict:
        """Validate and persist a proposal without changing source files."""
        SessionStore(self.ws).guard(session_id, "patch_proposal", override_reason)
        if SessionStore(self.ws).get(session_id)["task_id"] != task_id:
            raise PilotError("Patch task does not match this conversation", "wrong_task")
        submitted = proposal
        proposal = self.normalize(submitted)
        input_format = "file_contents" if "files" in submitted else "unified_diff"
        requested = set(submitted.get("files", {}))
        self.validate(proposal)
        patch_id = uid("patch")
        record = {
            "id": patch_id,
            "proposal": proposal,
            "input_format": input_format,
            "unchanged_files": sorted(requested - set(proposal["base_hashes"])),
            "session_id": session_id,
            "task_id": task_id,
            "status": "staged",
            "created_at": now(),
            "reviewed": False,
        }
        self.ws.put("patch", patch_id, record)
        atomic_json(self.ws.state / "patches" / patch_id / "proposal.json", record)
        atomic_bytes(self.ws.state / "patches" / patch_id / "proposal.diff", proposal["diff"].encode())
        return {"patch_id": patch_id, "status": "staged", "input_format": input_format,
                "changed_files": list(proposal["base_hashes"]), "unchanged_files": record["unchanged_files"],
                "next": f"repopilotv3 patch review {patch_id}"}

    def review(self, patch_id: str) -> dict:
        """Return the exact staged diff and record that local review was requested."""
        record = self.ws.get("patch", patch_id)
        self.validate(record["proposal"])
        record["reviewed"] = True
        record["reviewed_at"] = now()
        self.ws.put("patch", patch_id, record)
        return {
            "patch_id": patch_id,
            "diff": record["proposal"]["diff"],
            "status": record["status"],
            "next": f"repopilotv3 patch apply {patch_id} --confirm-reviewed",
        }

    def apply(self, patch_id: str, confirmed: bool, override_reason: str | None = None) -> dict:
        """Apply a confirmed reviewed patch and roll back recoverable write failures."""
        record = self.ws.get("patch", patch_id)
        if record["status"] != "staged":
            raise PilotError("Patch is not staged", "conflict")
        if not confirmed or not record["reviewed"]:
            raise PilotError("Review the patch first, then use --confirm-reviewed", "confirmation_required")
        SessionStore(self.ws).guard(record["session_id"], "patch_apply", override_reason)
        outputs = self.validate(record["proposal"])
        originals = {}
        modes = {}
        recovery = self.ws.state / "patches" / patch_id / "recovery"
        for relative in outputs:
            path = safe_path(self.ws.root, relative)
            original = path.read_bytes() if path.exists() else None
            actual_hash = digest(original) if original is not None else None
            if actual_hash != record["proposal"]["base_hashes"][relative]:
                raise PilotError("Source changed before recovery-copy capture", "stale_patch")
            originals[relative] = original
            modes[relative] = path.stat().st_mode if path.exists() else None
            if original is not None:
                atomic_bytes(recovery / relative, original)
        journal = {
            "state": "prepared",
            "files": {p: digest(b) if b is not None else None for p, b in originals.items()},
            "outputs": {p: digest(b) if b is not None else None for p, b in outputs.items()},
            "modes": modes,
        }
        atomic_json(recovery / "journal.json", journal)
        applied = []
        try:
            for relative, value in outputs.items():
                path = safe_path(self.ws.root, relative)
                current = path.read_bytes() if path.exists() else None
                if current != originals[relative]:
                    raise PilotError("Source changed immediately before patch application", "stale_patch")
                # Add before mutation: a failure after os.replace still participates in recovery.
                applied.append(relative)
                if value is None:
                    path.unlink()
                else:
                    mode = path.stat().st_mode if path.exists() else None
                    atomic_bytes(path, value)
                    if mode is not None:
                        os.chmod(path, mode)
        except BaseException:
            errors = []
            for relative in reversed(applied):
                try:
                    path = safe_path(self.ws.root, relative)
                    original = originals[relative]
                    current = path.read_bytes() if path.exists() else None
                    if current not in (outputs[relative], original):
                        errors.append(f"{relative}: changed by another editor; manual recovery required")
                        continue
                    if original is None:
                        path.unlink(missing_ok=True)
                    else:
                        atomic_bytes(path, original)
                        if modes[relative] is not None:
                            os.chmod(path, modes[relative])
                except OSError as exc:
                    errors.append(f"{relative}: {exc}")
            journal.update(state="recovery_required" if errors else "rolled_back", errors=errors)
            atomic_json(recovery / "journal.json", journal)
            if errors:
                raise PilotError(f"Patch recovery needed; inspect {recovery}", "recovery_required") from None
            raise
        journal["state"] = "applied"
        atomic_json(recovery / "journal.json", journal)
        record.update(status="applied", applied_at=now())
        self.ws.put("patch", patch_id, record)
        task = TaskStore(self.ws).get(record["task_id"])
        task["files"] = list(dict.fromkeys(task["files"] + list(outputs)))
        task["checkpoint_required"] = True
        task["revision"] += 1
        TaskStore(self.ws).save(task)
        snapshot = RepositoryIndex(self.ws).refresh()
        return {
            "patch_id": patch_id,
            "status": "applied",
            "snapshot": snapshot["id"],
            "next": "Run explicit validation, then record a progress update.",
        }

    def recover(self, patch_id: str) -> dict:
        """Restore an interrupted patch without overwriting later developer edits."""
        from repopilotv3.core import load_json

        record = self.ws.get("patch", patch_id)
        recovery = self.ws.state / "patches" / patch_id / "recovery"
        journal = load_json(recovery / "journal.json")
        if not journal or journal["state"] not in {"prepared", "recovery_required"}:
            raise PilotError("No interrupted application requires recovery")
        # Preflight all files before restoring. Never overwrite edits made after an interruption.
        backups = {}
        for relative, old_hash in journal["files"].items():
            path = safe_path(self.ws.root, relative)
            actual = digest(path.read_bytes()) if path.exists() else None
            if actual not in {old_hash, journal["outputs"][relative]}:
                raise PilotError(f"Recovery would overwrite a later edit: {relative}", "conflict")
            if old_hash is not None:
                data = (recovery / relative).read_bytes()
                if digest(data) != old_hash:
                    raise PilotError("Recovery backup is corrupt", "artifact_corrupt")
                backups[relative] = data
        for relative, old_hash in journal["files"].items():
            path = safe_path(self.ws.root, relative)
            if old_hash is None:
                path.unlink(missing_ok=True)
            else:
                atomic_bytes(path, backups[relative])
                if journal.get("modes", {}).get(relative) is not None:
                    os.chmod(path, journal["modes"][relative])
        journal["state"] = "rolled_back"
        atomic_json(recovery / "journal.json", journal)
        record["status"] = "recovered"
        self.ws.put("patch", patch_id, record)
        return {"patch_id": patch_id, "status": "recovered"}
