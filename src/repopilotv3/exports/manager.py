"""Consistent immutable Markdown generations and scoped confirmed-upload receipts."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from pathlib import Path

from repopilotv3.core import PilotError, atomic_bytes, atomic_json, canonical, digest, load_json, now, uid
from repopilotv3.exports.instructions import ProjectInstructions
from repopilotv3.manual.schema import REQUEST_SCHEMA, protocol_reference
from repopilotv3.manual.sessions import SessionStore, measure
from repopilotv3.manual.validation import ValidationEvidence
from repopilotv3.repository.index import RepositoryIndex, paths
from repopilotv3.repository.service import bounded
from repopilotv3.tasks.store import TaskStore


class ExportManager:
    """Render shared snapshots into stable reusable artifacts and chat-specific packets."""

    def __init__(self, ws):
        """Choose private generation storage without modifying external WebUI state."""
        self.ws = ws
        self.index = RepositoryIndex(ws)
        self.base = ws.state / "exports"

    def instructions(self) -> str:
        """Load standalone project behavior within its independent 8,000-character budget."""
        return ProjectInstructions().render()

    def template_fingerprint(self) -> str:
        """Invalidate publication reuse when instructions, protocol or request schemas change."""
        return digest(canonical([ProjectInstructions.REVISION, self.instructions(), protocol_reference(), REQUEST_SCHEMA]))

    def command_evidence(self, task_id: str | None, snapshot: str) -> list[dict]:
        """Carry compact actual execution facts into a handoff without exporting prior chat/log bodies."""
        if not task_id:
            return []
        with self.ws.db() as db:
            rows = db.execute("SELECT value FROM records WHERE kind='command' AND json_extract(value,'$.task_id')=? AND json_extract(value,'$.finished_at') IS NOT NULL ORDER BY json_extract(value,'$.finished_at') DESC LIMIT 5", (task_id,)).fetchall()
        result = []
        for row in rows:
            command = json.loads(row[0])
            result.append({"command_id": command["command_id"], "originating_chat": command["session_id"],
                           "argv_preview": bounded({"argv": command["argv"]}, 150)["argv"],
                           "status": command["status"], "exit_code": command["exit_code"],
                           "snapshot": command.get("snapshot"), "evidence_kind": "command",
                           "historical": command.get("snapshot") != snapshot or bool(command.get("source_observation_incomplete")),
                           "note": "Actual command execution, not executed-test proof. Full logs remain in the originating chat."})
        return result

    def bootstrap(self, session: dict, task: dict, snap: dict) -> str:
        """Continue from state and verified context without repeating the project prompt."""
        evidence = ValidationEvidence(self.ws).inspect(snap["id"], task_id=task["id"])
        concise = bounded({"validation": evidence, "commands": self.command_evidence(task["id"], snap["id"])}, 1500)
        return (
            f"# Continue task {task['id']}\n\nLocal chat: `{session['id']}`\n"
            f"Snapshot: `{snap['id']}`\n\n{TaskStore(self.ws).render(task)}\n\n"
            "## Recorded validation\n\n```json\n" + json.dumps(concise, indent=2) + "\n```\n\n"
            "## Next actions\n\nStart by calling todo with the task ID above. Choose the next unresolved item. "
            "Use the project instructions and protocol reference. Local files are accessible only through "
            "uploaded context or returned JSON tools. Inspect upload checklist for current files.\n"
        )

    def prepare(self, session_id: str, full_source: bool = False) -> dict:
        """Publish one consistent generation; readers follow an atomically replaced pointer."""
        session = SessionStore(self.ws).get(session_id)
        task = TaskStore(self.ws).get(session["task_id"]) if session["task_id"] else None
        snap = self.index.refresh()
        source = self.index.files(snap["id"])
        selected = self.index.select(task, snap) if task else []
        bodies = {
            "00_PROJECT_INSTRUCTIONS.md": (self.instructions(), {}),
            "07_PROTOCOL_REFERENCE.md": (protocol_reference(), {}),
            "request.schema.json": (json.dumps(REQUEST_SCHEMA, indent=2) + "\n", {}),
        }
        overview = [
            "# Repository map",
            "",
            f"Repository: {self.ws.root.name}",
            "Analysis: " + snap["quality"],
            "Declared project metadata: " + canonical(bounded({"projects": snap["projects"]}, 600)),
            "",
            "## Files and source mapping",
            "",
        ]
        mapped = 0
        for path in source:
            if measure("\n".join(overview) + path)["estimated_tokens"] > 1100:
                break
            overview.append(f"- `{path}`")
            mapped += 1
        if mapped < len(source):
            overview.append(f"{len(source) - mapped} further paths are in 08_SOURCE_MAP.md.")
        bodies["08_SOURCE_MAP.md"] = (
            "# Complete eligible source map\n\n" + "\n".join(f"- `{p}`" for p in source) + "\n",
            snap["hashes"],
        )
        overview += ["", "## Public declarations", ""]
        for symbol in snap["symbols"][:150]:
            line = (
                f"- `{symbol['path']}:{symbol.get('start', 1)}` — {symbol.get('signature', symbol['name'])}"
            )
            if measure("\n".join(overview) + line)["estimated_tokens"] > 1500:
                overview.append("Further declarations are available through search/context.")
                break
            overview.append(line)
        overview += [
            "",
            "## Capabilities and constraints",
            "",
            *[d[:160] for d in snap["diagnostics"][:3]],
            "",
            "Build/test profiles: " + ", ".join(self.ws.config["profiles"]),
            "Source excludes private state, ignored/generated output, binaries, sensitive filenames, "
            "non-UTF-8 files, symlinks and files exceeding the configured byte limit.",
        ]
        bodies["01_REPOSITORY_MAP.md"] = ("\n".join(overview) + "\n", snap["hashes"])
        if task:
            focus = ["# Focused task context", "", task["objective"], "", "## Selected source paths", ""]
            for path in selected:
                focus.append(f"- `{path}` sha256 `{snap['hashes'][path]}`")
            chosen_ids = {s["id"] for s in snap["symbols"] if s["path"] in selected}
            focus += [
                "",
                "## Declarations and relationships",
                "",
                canonical(
                    bounded(
                        {
                            "symbols": [s for s in snap["symbols"] if s["id"] in chosen_ids],
                            "relationships": [
                                r
                                for r in snap["relationships"]
                                if r["source"] in chosen_ids or r["target"] in chosen_ids
                            ],
                        },
                        5000,
                    )
                ),
            ]
            deps = {p: snap["hashes"][p] for p in selected}
            bodies["02_FOCUSED_CONTEXT.md"] = ("\n".join(focus) + "\n", deps)
            implementation, omissions = ["# Task implementation source\n"], []
            for path in selected:
                unit = self.source_markdown(path, source[path], snap["hashes"][path])
                if measure("\n".join(implementation) + unit)["estimated_tokens"] > 10000:
                    omissions.append(path)
                else:
                    implementation.append(unit)
            implementation.append(
                "\nOmitted complete files (use read/context or full-source exports): " + canonical(omissions)
            )
            bodies["03_IMPLEMENTATION_CONTEXT.md"] = ("\n".join(implementation), deps)
            evidence = bounded(
                {"validation": ValidationEvidence(self.ws).inspect(snap["id"], task_id=task["id"]),
                 "commands": self.command_evidence(task["id"], snap["id"])}, 2000
            )
            bodies["04_VALIDATION_EVIDENCE.md"] = (
                "# Local validation evidence\n\nCurrent snapshot: `"
                + snap["id"]
                + "`\n\n```json\n"
                + json.dumps(evidence, indent=2)
                + "\n```\n\nModel progress is not verified evidence.\n",
                {},
            )
            bodies["05_TODO.md"] = (TaskStore(self.ws).render(task), {})
            bodies["06_CONTINUATION.md"] = (self.bootstrap(session, task, snap), {})
        if full_source:
            for path, data in source.items():
                name = "full/" + digest(path)[:12] + "_" + Path(path).name[:100] + ".md"
                bodies[name] = (
                    self.source_markdown(path, data, snap["hashes"][path]),
                    {path: snap["hashes"][path]},
                )
        generation_id = uid("generation")
        destination = self.base / generation_id
        destination.mkdir(parents=True)
        manifest = {
            "id": generation_id,
            "created_at": now(),
            "snapshot": snap["id"],
            "head": snap["head"],
            "session_id": session_id,
            "task_id": session["task_id"],
            "project": session["project"],
            "full_source": full_source,
            "artifacts": {},
            "eligibility": {
                "indexed": len(source),
                "candidate_count": len(paths(self.ws)),
                "oversized_or_non_utf8": sorted(set(paths(self.ws)) - source.keys()),
                "excluded": "Git ignores, private state, generated/binary/sensitive names and symlinks",
            },
        }
        published = False
        try:
            for name, (body, dependencies) in bodies.items():
                semantic_key = digest(body)
                cache_key = digest(canonical(["plain-project-instructions", ProjectInstructions.REVISION, semantic_key])) if name == "00_PROJECT_INSTRUCTIONS.md" else semantic_key
                cache = self.base / "artifacts" / cache_key
                cached = load_json(cache.with_suffix(".json"))
                if cached:
                    data = cache.with_suffix(".data").read_bytes()
                    if digest(data) != cached["hash"]:
                        raise PilotError("Cached immutable artifact is corrupt", "artifact_corrupt")
                    artifact = {**cached, "dependencies": dependencies, "verified_snapshot": snap["id"]}
                else:
                    header = (
                        f"<!-- created {now()}; creation snapshot {snap['id']}; "
                        f"git HEAD {snap['head']}; body fingerprint {semantic_key} -->\n\n"
                    )
                    data = (body if name.endswith(".json") or name == "00_PROJECT_INSTRUCTIONS.md" else header + body).encode("utf-8")
                    artifact = {
                        "hash": digest(data),
                        "body_hash": semantic_key,
                        "creation_snapshot": snap["id"],
                        "dependencies": dependencies,
                        "verified_snapshot": snap["id"],
                    }
                    atomic_bytes(cache.with_suffix(".data"), data)
                    atomic_json(cache.with_suffix(".json"), artifact)
                if name == "00_PROJECT_INSTRUCTIONS.md":
                    ProjectInstructions().check_budget(data.decode("utf-8"))
                atomic_bytes(destination / name, data)
                manifest["artifacts"][name] = artifact
            # A second verified capture prevents publication across concurrent editor changes.
            if self.index.refresh()["id"] != snap["id"]:
                raise PilotError("Source changed while exporting; retry", "unstable_snapshot")
            manifest["directory"] = str(destination)
            atomic_json(destination / "manifest.json", manifest)
            checklist = self.checklist(manifest)
            atomic_bytes(destination / "UPLOAD_CHECKLIST.md", self.render_checklist(checklist).encode())
            atomic_json(self.base / f"current-{session_id}.json", manifest)
            published = True
            try:
                self.ws.put("export", session_id, manifest)
            except sqlite3.Error:
                pass
        except BaseException:
            # Failed unpublished generations are safe to remove; previous pointer stays valid.
            if not published:
                shutil.rmtree(destination, ignore_errors=True)
            raise
        from repopilotv3.exports.cleanup import ExportCleanup

        return {"manifest": manifest, "checklist": checklist, "directory": str(destination),
                "cleanup": ExportCleanup(self.ws).run()}

    def source_markdown(self, path: str, data: bytes, hash_: str) -> str:
        """Preserve an entire source file with its original path and full line range."""
        text = data.decode("utf-8-sig")
        fence = "`" * max(3, max((len(x) for x in re.findall(r"`+", text)), default=0) + 1)
        return f"# {path}\n\nSource: `{path}:1-{len(text.splitlines())}`; sha256 `{hash_}`\n\n{fence}\n{text}\n{fence}\n"

    def scope(self, manifest: dict, name: str) -> str:
        """Keep project instruction/map receipts separate from task/chat attachments."""
        return (
            "project:" + manifest["project"]
            if name.startswith(("00_", "01_", "07_", "08_", "full/", "request.schema"))
            else "chat:" + manifest["session_id"]
        )

    def checklist(self, manifest: dict) -> list[dict]:
        """Distinguish paste/upload/keep/replace using only explicit confirmations."""
        rows = []
        for name, artifact in manifest["artifacts"].items():
            scope = self.scope(manifest, name)
            key = digest(canonical([scope, name]))
            try:
                receipt = self.ws.get("upload", key)
            except PilotError:
                receipt = None
            current = receipt and receipt["hash"] == artifact["hash"]
            rows.append(
                {
                    "file": name,
                    "hash": artifact["hash"],
                    "scope": scope,
                    "action": "keep"
                    if current
                    else "replace"
                    if receipt
                    else "paste"
                    if name.startswith("00_")
                    else "upload",
                    "state": "current"
                    if current
                    else "changed since upload"
                    if receipt
                    else "never uploaded",
                    "superseded_hash": receipt["hash"] if receipt and not current else None,
                    "required": not name.startswith(("full/", "request.schema", "08_")),
                }
            )
        return rows

    def confirm(self, generation_id: str, session_id: str, confirmations: dict) -> list[dict]:
        """Confirm exact selected hashes from one generation in one explicit scope."""
        manifest = load_json(self.base / f"current-{session_id}.json")
        if not manifest:
            raise PilotError("Prepare context first", "not_found")
        if manifest["id"] != generation_id:
            raise PilotError("Generation changed; refresh the upload checklist", "stale_export")
        for name, expected in confirmations.items():
            artifact = manifest["artifacts"].get(name)
            if not artifact or artifact["hash"] != expected:
                raise PilotError("Confirm the exact exported artifact version", "stale_export")
            data = (Path(manifest["directory"]) / name).read_bytes()
            if digest(data) != expected:
                raise PilotError("Export file was modified", "artifact_corrupt")
        for name, expected in confirmations.items():
            scope = self.scope(manifest, name)
            self.ws.put("upload", digest(canonical([scope, name])), {"hash": expected, "at": now()})
            SessionStore(self.ws).record(
                session_id,
                "transfer:" + expected,
                "upload",
                (Path(manifest["directory"]) / name).read_text("utf-8"),
            )
        return self.checklist(manifest)

    def render_checklist(self, rows: list[dict]) -> str:
        """Render ordered upload actions with explicit external replacement guidance."""
        return (
            "# Paste / upload / keep / replace\n\n"
            + "\n".join(
                f"- {r['action'].upper()}: {r['file']} — {r['state']}"
                + (" (optional)" if not r["required"] else "")
                for r in rows
            )
            + "\n\nRemove superseded external files when possible. If replacement is unavailable, use a fresh chat/project. "
            "Only confirm files actually transferred. The manifest snapshot verifies dependencies for reused artifacts.\n"
        )
