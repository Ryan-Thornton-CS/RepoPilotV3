"""One immutable, searchable repository index shared by all frontends."""

from __future__ import annotations

import ast
import fnmatch
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

from repopilotv3.core import (
    PilotError,
    Workspace,
    canonical,
    digest,
    dotnet_env,
    git,
    map_root,
    now,
    run,
)

TEXT_SUFFIXES = {
    ".c",
    ".h",
    ".cpp",
    ".hpp",
    ".go",
    ".rs",
    ".java",
    ".kt",
    ".kts",
    ".rb",
    ".php",
    ".html",
    ".css",
    ".scss",
    ".vue",
    ".svelte",
    ".razor",
    ".cshtml",
    ".vb",
    ".fs",
    ".vbproj",
    ".fsproj",
    ".vcxproj",
    ".slnf",
    ".gradle",
    ".cmake",
    ".cs",
    ".csproj",
    ".sln",
    ".slnx",
    ".props",
    ".targets",
    ".json",
    ".xml",
    ".config",
    ".md",
    ".txt",
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".resx",
    ".xaml",
    ".addin",
    ".sql",
    ".sh",
    ".ps1",
}
SKIP_DIRS = {
    ".git",
    ".repopilotv2",
    ".repopilotv3",
    ".repopilot",
    "build",
    ".tox",
    "venv",
    ".venv",
    "node_modules",
    "bin",
    "obj",
    "__pycache__",
    "dist",
    ".pytest_cache",
    ".ruff_cache",
    ".tools",
    "TestResults",
    ".idea",
    ".vs",
    ".vscode-server",
    ".vscode-server-insiders",
    ".vscode-test",
    ".cache",
    ".local",
    ".codex",
    ".npm",
    ".nuget",
    ".dotnet",
    ".ssh",
    ".aws",
    ".azure",
    ".config",
    ".cargo",
    ".rustup",
}
SECRET_NAMES = {
    ".env",
    ".git-credentials",
    "credentials",
    "id_rsa",
    "id_ed25519",
    "credentials.json",
    "secrets.json",
    "appsettings.Production.json",
}


def allowed(ws: Workspace, path: str, config: dict | None = None) -> bool:
    """Check source inclusion rules, private paths, secret names, and symlinks."""
    p = Path(path)
    if any(x.casefold() in {d.casefold() for d in SKIP_DIRS} for x in p.parts) or p.name.casefold() in {
        n.casefold() for n in SECRET_NAMES
    }:
        return False
    if p.name.startswith(".env") or p.suffix.lower() in {".key", ".pem", ".pfx", ".p12"}:
        return False
    if p.suffix.lower() not in TEXT_SUFFIXES and p.name not in {"Dockerfile", ".gitignore", ".editorconfig"}:
        return False
    full = ws.root / p
    if full.is_relative_to(ws.state) or any(x.is_symlink() for x in [full, *full.parents]):
        return False
    return not any(fnmatch.fnmatch(path, pat) for pat in (config if config is not None else ws.config)["exclude"])


def paths(ws: Workspace) -> list[str]:
    """Enumerate source using one configuration read and honor nested repository ignores."""
    config = ws.config
    skipped = {s.casefold() for s in SKIP_DIRS}
    if git(ws.root, "rev-parse", "--show-toplevel").strip():
        raw = git(ws.root, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
        candidates = raw.split("\0")
    else:
        candidates = []
        for root, dirs, files in os.walk(ws.root, followlinks=False):
            directory = Path(root)
            if directory != ws.root and (directory / ".git").exists() and not (directory / ".git").is_symlink():
                prefix = directory.relative_to(ws.root).as_posix() + "/"
                nested = git(directory, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
                candidates.extend(prefix + name for name in nested.split("\0") if name)
                dirs[:] = []
                continue
            dirs[:] = [d for d in dirs if d.casefold() not in skipped
                       and not (Path(root) / d).is_symlink()
                       and not (Path(root) / d).is_relative_to(ws.state)
                       and not any(fnmatch.fnmatch((Path(root) / d).relative_to(ws.root).as_posix() + "/", pat)
                                   for pat in config["exclude"])]
            candidates.extend((Path(root) / f).relative_to(ws.root).as_posix() for f in files)
    return sorted({p for p in candidates if p and allowed(ws, p, config)})


def capture(ws: Workspace) -> dict[str, bytes]:
    """Read bounded UTF-8 source files as original bytes for a consistent snapshot."""
    result = {}
    max_size = ws.config["max_file_bytes"]
    for name in paths(ws):
        path = ws.root / name
        try:
            if not path.is_file() or path.stat().st_size > max_size:
                continue
            data = path.read_bytes()
            if len(data) > max_size or b"\0" in data:
                continue
            data.decode("utf-8-sig")
            result[name] = data
        except (FileNotFoundError, UnicodeDecodeError):
            continue
    return result


def generated_sources(ws: Workspace, files: dict) -> dict[str, bytes]:
    """Capture generated C# compiler inputs for analysis only, excluding them from exports/search."""
    result = {}
    for name in files:
        if name.endswith(".csproj"):
            obj = (ws.root / name).parent / "obj"
            candidates = list(obj.glob("**/*.cs"))
            assets = obj / "project.assets.json"
            if assets.is_file():
                # SDK packages can supply compile inputs outside the repository, e.g. VSTest's Main.
                try:
                    restored = json.loads(assets.read_text("utf-8-sig"))
                    for package in restored.get("libraries", {}).values():
                        if package.get("type") != "package":
                            continue
                        for folder in restored.get("packageFolders", {}):
                            base = Path(folder) / package.get("path", "")
                            for scope in ["build", "buildTransitive", "buildMultiTargeting"]:
                                candidates.extend((base / scope).glob("**/*.cs"))
                except (ValueError, OSError):
                    pass
            for p in candidates:
                if (
                    p.is_file()
                    and not any(x.is_symlink() for x in [p, *p.parents])
                    and p.stat().st_size <= ws.config["max_file_bytes"]
                ):
                    try:
                        data = p.read_bytes()
                        data.decode("utf-8-sig")
                        key = (
                            p.relative_to(ws.root).as_posix()
                            if p.is_relative_to(ws.root)
                            else str(p.resolve())
                        )
                        result[key] = data
                    except (OSError, UnicodeDecodeError):
                        continue
    return result


def project_metadata(files: dict[str, bytes]) -> list[dict]:
    """Extract declared frameworks, references, and descriptions without evaluating or rewriting project XML."""
    result = []
    for path, content in files.items():
        if not path.endswith(".csproj"):
            continue
        try:
            root = ET.fromstring(content)
        except ET.ParseError:
            result.append(
                {"name": Path(path).stem, "path": path, "declared_metadata_error": "Invalid project XML"}
            )
            continue
        properties = {}
        references, dependencies = [], []
        for element in root.iter():
            tag = element.tag.rsplit("}", 1)[-1]
            if tag in {
                "TargetFramework",
                "TargetFrameworks",
                "TargetFrameworkVersion",
                "Description",
                "RootNamespace",
                "LangVersion",
                "PlatformTarget",
            }:
                properties.setdefault(tag, []).append((element.text or "").strip())
            if tag == "Reference":
                references.append(element.attrib.get("Include", ""))
            if tag == "ProjectReference":
                dependencies.append(Path(element.attrib.get("Include", "").replace("\\", "/")).stem)
        result.append(
            {
                "name": Path(path).stem,
                "path": path,
                "sdk": root.attrib.get("Sdk"),
                "declared_properties": properties,
                "dependencies": dependencies,
                "assembly_references": references,
                "inventor_integration": any("inventor" in x.lower() for x in references),
                "metadata_note": "Declared XML values; conditional/property expressions may require MSBuild evaluation.",
            }
        )
    return result


class RepositoryIndex:
    """Maintain immutable repository snapshots and one shared FTS5/semantic index."""

    def __init__(self, ws: Workspace):
        """Attach the index to its workspace storage and configuration."""
        self.ws = ws

    def refresh(self, attempts: int = 3, force: bool = False) -> dict:
        """Capture and validate stable source bytes, then publish an indexed snapshot."""
        self.ws.config
        with self.ws.db() as db:
            db.execute("CREATE TABLE IF NOT EXISTS contents(hash TEXT PRIMARY KEY, content BLOB NOT NULL)")
            db.execute(
                "CREATE TABLE IF NOT EXISTS search_documents(path TEXT, hash TEXT, PRIMARY KEY(path,hash))"
            )
        try:
            previous_snapshot = self.current(max_bytes=32 * 1024 * 1024)
        except PilotError:
            previous_snapshot = None
        for _ in range(attempts):
            config = self.ws.config
            files = capture(self.ws)
            auxiliary = generated_sources(self.ws, files)
            hashes = {p: digest(b) for p, b in files.items()}
            head = git(self.ws.root, "rev-parse", "HEAD").strip() or None
            analysis_key = {
                k: config[k]
                for k in [
                    "analysis",
                    "dotnet",
                    "worker_command",
                    "path_mappings",
                    "interop_paths",
                    "reference_assembly_paths",
                ]
            }
            analysis_key["engine_fingerprint"] = digest(
                Path(__file__).read_bytes() + (Path(__file__).parents[1] / "analysis/roslyn.py").read_bytes()
            )
            analysis_key["sdk_host_hash"] = (
                digest(Path(config["dotnet"]).read_bytes())
                if config["dotnet"] and Path(config["dotnet"]).is_file()
                else None
            )
            analysis_key["generated_hashes"] = {p: digest(b) for p, b in auxiliary.items()}
            # Restore artifacts affect semantic resolution even though generated output is never exported.
            analysis_key["restore_inputs"] = {}
            for name in files:
                if name.endswith(".csproj"):
                    obj = (self.ws.root / name).parent / "obj"
                    for generated in [
                        obj / "project.assets.json",
                        *obj.glob("*.nuget.g.props"),
                        *obj.glob("*.nuget.g.targets"),
                    ]:
                        if generated.is_file() and not generated.is_symlink():
                            analysis_key["restore_inputs"][str(generated)] = digest(generated.read_bytes())
            external_hashes = {}
            for entry in config["interop_paths"] + config["reference_assembly_paths"]:
                path = Path(entry)
                if path.is_file():
                    external_hashes[str(path)] = digest(path.read_bytes())
            analysis_key["external_hashes"] = external_hashes
            snapshot_id = digest(canonical({"files": hashes, "head": head, "analysis": analysis_key}))
            with self.ws.db() as db:
                existing = db.execute("SELECT metadata FROM snapshots WHERE id=?", (snapshot_id,)).fetchone()
            if existing and not force:
                analysis = json.loads(existing[0])
            else:
                csharp_inputs = {
                    p: h
                    for p, h in hashes.items()
                    if p.endswith((".cs", ".csproj", ".sln", ".slnx", ".props", ".targets", "global.json"))
                }
                previous_inputs = {
                    p: h
                    for p, h in (previous_snapshot or {}).get("hashes", {}).items()
                    if p.endswith((".cs", ".csproj", ".sln", ".slnx", ".props", ".targets", "global.json"))
                }
                if (
                    not force
                    and previous_snapshot
                    and csharp_inputs == previous_inputs
                    and previous_snapshot["analysis_configuration"] == analysis_key
                ):
                    semantic = {
                        k: previous_snapshot[k]
                        for k in [
                            "quality",
                            "symbols",
                            "relationships",
                            "projects",
                            "diagnostics",
                            "document_hashes",
                        ]
                    }
                    semantic["symbols"] = [s for s in semantic["symbols"] if not s["path"].endswith(".py")]
                else:
                    try:
                        semantic = self._analyze(files, hashes, {**config, "_generated_sources": auxiliary})
                    except PilotError as exc:
                        semantic = {
                            "quality": "text_only",
                            "symbols": [],
                            "relationships": [],
                            "projects": [],
                            "document_hashes": {},
                            "diagnostics": [f"Semantic analysis unavailable: {exc}"],
                        }
                semantic["symbols"] = semantic["symbols"] + python_symbols(files)

                evaluated = {p.get("path"): p for p in semantic.get("projects", []) if p.get("path")}
                semantic["projects"] = [
                    {**p, **evaluated.get(p["path"], {})} for p in project_metadata(files)
                ] + [p for p in semantic.get("projects", []) if not p.get("path")]
                analysis = {
                    "id": snapshot_id,
                    "created_at": now(),
                    "head": head,
                    "hashes": hashes,
                    "analysis_configuration": analysis_key,
                    **semantic,
                }
            # Validate the worker's input and the entire capture, including additions and deletions.
            if files != capture(self.ws) or head != (git(self.ws.root, "rev-parse", "HEAD").strip() or None):
                continue
            if auxiliary != generated_sources(self.ws, files):
                continue
            if self.ws.config != config:
                continue
            # Recheck reference and restore inputs, not just visible source text.
            if any(
                not Path(p).is_file() or digest(Path(p).read_bytes()) != h for p, h in external_hashes.items()
            ):
                continue
            if any(
                not Path(p).is_file() or digest(Path(p).read_bytes()) != h
                for p, h in analysis_key["restore_inputs"].items()
            ):
                continue
            if not existing or force:
                with self.ws.db() as db:
                    db.execute(
                        "INSERT INTO snapshots VALUES(?,?) ON CONFLICT(id) DO UPDATE SET metadata=excluded.metadata",
                        (snapshot_id, canonical(analysis)),
                    )
                    if not existing:
                        for p, content in files.items():
                            db.execute("INSERT OR IGNORE INTO contents VALUES(?,?)", (hashes[p], content))
                            db.execute("INSERT INTO files VALUES(?,?,?,NULL)", (snapshot_id, p, hashes[p]))
                            indexed = db.execute(
                                "INSERT OR IGNORE INTO search_documents VALUES(?,?)", (p, hashes[p])
                            ).rowcount
                            if indexed:
                                db.execute(
                                    "INSERT INTO search(path,body,snapshot) VALUES(?,?,?)",
                                    (p, content.decode("utf-8-sig"), hashes[p]),
                                )
            self.ws.put("meta", "current_snapshot", {"id": snapshot_id})
            if previous_snapshot and previous_snapshot["hashes"] != hashes:
                self._notify_tasks(previous_snapshot["hashes"], hashes)
            return analysis
        raise PilotError("Repository changed during capture; retry after edits settle", "unstable_snapshot")

    def _notify_tasks(self, previous_hashes: dict, current_hashes: dict) -> None:
        """Require progress after relevant changes made by an editor, Copilot, or imported patch."""
        from repopilotv3.tasks.store import TaskStore

        changed = {
            p
            for p in previous_hashes.keys() | current_hashes.keys()
            if previous_hashes.get(p) != current_hashes.get(p)
        }
        for task in TaskStore(self.ws).list():
            relevant = changed.intersection(task["files"] + task["seeds"] + task["pins"])
            if relevant:
                task["checkpoint_required"] = True
                task["files"] = list(dict.fromkeys(task["files"] + sorted(relevant)))
                TaskStore(self.ws).save(task)

    def _analyze(self, files: dict, hashes: dict, config: dict) -> dict:
        """Run the isolated Python Roslyn worker or report text-only analysis honestly."""
        if not any(p.endswith(".cs") for p in files):
            return {
                "quality": "text_only",
                "symbols": [],
                "relationships": [],
                "projects": [],
                "diagnostics": ["No C# documents; text search and bounded exports remain available."],
                "document_hashes": {},
            }
        command = config["worker_command"]
        if not command and config["dotnet"]:
            command = [sys.executable, "-m", "repopilotv3.analysis.roslyn"]
        if command:
            import subprocess

            request = {
                "version": "1.0",
                "operation": "analyze",
                "root": map_root(self.ws.root, config["path_mappings"]),
                "files": [
                    {
                        "path": p,
                        "hash": digest(b),
                        "text": b.decode("utf-8-sig"),
                        "text_hash": digest(b.decode("utf-8-sig")),
                        "auxiliary": p not in files,
                    }
                    for p, b in {**files, **config.get("_generated_sources", {})}.items()
                    if p.endswith(".cs")
                ],
                "sdk_executable": config["dotnet"],
                "projects": [p for p in files if p.endswith(".csproj")],
                "configuration": config["analysis"],
                "interop_paths": config["interop_paths"],
                "reference_assembly_paths": config["reference_assembly_paths"],
            }
            try:
                result = subprocess.run(
                    command,
                    input=canonical(request) + "\n",
                    text=True,
                    encoding="utf-8",
                    capture_output=True,
                    cwd=self.ws.root,
                    timeout=180,
                    env=dotnet_env(config["dotnet"]),
                    shell=False,
                )
                if result.returncode:
                    raise PilotError(result.stderr[-4000:] or "Roslyn worker failed", "worker_failed")
                response = json.loads(result.stdout.strip())
                if response.get("error"):
                    raise PilotError(response["error"], "worker_failed")
                expected = {p: h for p, h in hashes.items() if p.endswith(".cs")}
                if response.get("document_hashes") != expected:
                    raise PilotError(
                        "Worker document checksums differ from captured input", "worker_snapshot_mismatch"
                    )
                return response
            except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
                raise PilotError(f"Cannot use configured Roslyn worker: {exc}", "worker_failed") from exc
        return {
            "quality": "text_only",
            "symbols": [],
            "relationships": [],
            "projects": [],
            "diagnostics": [
                "Semantic C# analysis unavailable; configure a local SDK and the csharp dependencies."
            ],
            "document_hashes": {p: h for p, h in hashes.items() if p.endswith(".cs")},
        }

    def current_id(self) -> str:
        """Read freshness identity without loading any declarations or source metadata."""
        return self.ws.get("meta", "current_snapshot")["id"]

    def current(self, max_bytes: int | None = None) -> dict:
        """Load the most recently published immutable snapshot metadata."""
        key = self.current_id()
        with self.ws.db() as db:
            row = db.execute("SELECT metadata FROM snapshots WHERE id=? AND (? IS NULL OR length(metadata)<=?)", (key, max_bytes, max_bytes)).fetchone()
        if not row:
            raise PilotError("Previous snapshot is absent or too large for incremental reuse; rebuild from current source.", "snapshot_cache_rebuild")
        return json.loads(row[0])

    def files(self, snapshot: str) -> dict[str, bytes]:
        """Retrieve original source bytes belonging to a specific snapshot."""
        with self.ws.db() as db:
            return {
                r[0]: bytes(r[1])
                for r in db.execute(
                    "SELECT f.path,COALESCE(f.content,c.content) FROM files f "
                    "LEFT JOIN contents c ON c.hash=f.hash WHERE f.snapshot=?",
                    (snapshot,),
                )
            }

    def query(self, text: str, limit: int = 12, snapshot: dict | None = None) -> dict:
        """Search bounded text and symbol results within one snapshot."""
        snap = snapshot or self.current()
        limit = max(1, min(limit, 50))
        terms = re.findall(r"\w+", text, re.UNICODE)[:24]
        expression = " OR ".join('"' + t + '"' for t in terms)
        hits = []
        if expression:
            with self.ws.db() as db:
                hits = [
                    dict(r)
                    for r in db.execute(
                        "SELECT search.path,snippet(search,1,'[',']',' … ',30) AS excerpt,bm25(search) AS rank "
                        "FROM search JOIN files f ON f.path=search.path AND f.hash=search.snapshot "
                        "WHERE search MATCH ? AND f.snapshot=? ORDER BY rank,search.path LIMIT ?",
                        (expression, snap["id"], limit),
                    )
                ]
        symbols = [s for s in snap["symbols"] if any(t.lower() in s["name"].lower() for t in terms)][:limit]
        for hit in hits:
            hit["hash"] = snap["hashes"][hit["path"]]
            hit["excerpt_truncated"] = len(hit["excerpt"]) > 3000
            hit["excerpt"] = hit["excerpt"][:3000]
        return {"snapshot": snap["id"], "quality": snap["quality"], "hits": hits, "symbols": symbols}

    def source(self, path: str, start: int = 1, end: int = 200, snapshot: dict | None = None) -> dict:
        """Return a confined source range with its hash and snapshot citation."""
        snap = snapshot or self.current()
        if start < 1 or end < start or end - start >= 500:
            raise PilotError("Source ranges must contain 1–500 lines")
        files = self.files(snap["id"])
        if path not in files:
            raise PilotError("Path is not in the permitted repository index", "not_found")
        lines = files[path].decode("utf-8-sig").splitlines()
        excerpt = "\n".join(lines[start - 1 : end])
        return {
            "snapshot": snap["id"],
            "path": path,
            "start": start,
            "end": min(end, len(lines)),
            "hash": snap["hashes"][path],
            "text": excerpt[:50000],
            "total_lines": len(lines),
            "truncated": len(excerpt) > 50000 or end < len(lines),
        }

    def select(self, task: dict, snap: dict) -> list[str]:
        """Rank task seeds, semantic neighbors, text matches, and related tests."""
        files = self.files(snap["id"])
        ranked = self.query(task["objective"], 50, snap)["hits"]
        paths_ = [p for p in task["pins"] + task["seeds"] if p in files]
        seeds = set(task["seeds"] + task["pins"])
        selected_symbols = {s["id"] for s in snap["symbols"] if s["name"] in seeds or s["path"] in paths_}
        for _ in range(2):
            previous = set(selected_symbols)
            for relation in snap["relationships"]:
                if relation["source"] in previous or relation["target"] in previous:
                    selected_symbols.update([relation["source"], relation["target"]])
        paths_.extend(s["path"] for s in snap["symbols"] if s["id"] in selected_symbols)
        paths_.extend(x["path"] for x in ranked)
        # Related tests by name in text fallback, actual references when Roslyn is available.
        names = {Path(p).stem for p in paths_}
        paths_.extend(p for p in files if "test" in p.lower() and any(n in p for n in names))
        return list(
            dict.fromkeys(p for p in paths_ if not any(fnmatch.fnmatch(p, pat) for pat in task["excludes"]))
        )[:50]


def changes(ws: Workspace) -> dict:
    """Read tracked changes and explain how untracked source can be retrieved."""
    candidates = git(ws.root, "diff", "HEAD", "--name-only", "-z", "--no-ext-diff", "--no-textconv").split(
        "\0"
    )
    permitted = [p for p in candidates if p and allowed(ws, p)][:200]
    diff = (
        git(ws.root, "diff", "HEAD", "--no-ext-diff", "--no-textconv", "--", *permitted) if permitted else ""
    )
    return {
        "status": git(ws.root, "status", "--short"),
        "diff": diff[:100000],
        "truncated": len(diff) > 100000 or len(candidates) > 200,
        "note": "Diff is against HEAD; untracked contents are available through indexed source queries.",
    }


def doctor(ws: Workspace) -> dict:
    """Report SDK, references, runner configuration, and degraded analysis capabilities."""
    c = ws.config
    checks = {"dotnet": c["dotnet"], "interop": {}, "reference_assemblies": {}, "warnings": []}
    if c["dotnet"]:
        try:
            checks["sdk"] = run([c["dotnet"], "--info"], ws.root, env=dotnet_env(c["dotnet"]))
        except PilotError as exc:
            checks["warnings"].append(str(exc))
    else:
        checks["warnings"].append("No SDK found. Set config.json dotnet to your user-space SDK executable.")
    for field, key in [("interop_paths", "interop"), ("reference_assembly_paths", "reference_assemblies")]:
        checks[key] = {p: Path(p).exists() for p in c[field]}
    checks["configuration"] = c["analysis"]
    checks["worker_command"] = c["worker_command"]
    checks["profiles"] = c["profiles"]
    checks["path_mappings"] = c["path_mappings"]
    checks["windows_validation"] = os.name == "nt"
    global_json = ws.root / "global.json"
    checks["test_runner"] = (
        json.loads(global_json.read_text()).get("test", {}).get("runner", "VSTest")
        if global_json.exists()
        else "VSTest"
    )
    checks["com_reference_projects"] = [
        p for p, b in capture(ws).items() if p.endswith(".csproj") and b"COMReference" in b
    ]
    if checks["com_reference_projects"]:
        checks["warnings"].append(
            "COMReference requires tasks unsupported by SDK MSBuild; use existing direct DLL references."
        )
    try:
        snap = RepositoryIndex(ws).current()
        checks.update(analysis_quality=snap["quality"], project_diagnostics=snap["diagnostics"])
    except PilotError:
        checks["warnings"].append("Index has not been created yet.")
    checks["offline_policy"] = (
        "No implicit restore/model/telemetry traffic; project targets are not network sandboxed."
    )
    return checks


def python_symbols(files: dict[str, bytes]) -> list[dict]:
    """Extract Python declarations and docstrings without importing project code."""
    symbols = []
    for path, body in files.items():
        if not path.endswith(".py"):
            continue
        try:
            tree = ast.parse(body.decode("utf-8-sig"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                symbols.append(
                    {
                        "id": f"{path}:{node.lineno}:{node.name}",
                        "name": node.name,
                        "path": path,
                        "start": node.lineno,
                        "end": node.end_lineno,
                        "signature": ast.get_source_segment(body.decode("utf-8-sig"), node).splitlines()[0],
                        "summary": ast.get_docstring(node) or "",
                        "kind": type(node).__name__,
                        "public": not node.name.startswith("_"),
                    }
                )
    return symbols
