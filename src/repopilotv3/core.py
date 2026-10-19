"""Configuration, durable storage, safe paths, and portable process execution."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any


class PilotError(Exception):
    """An operational failure with a stable machine-readable error code."""

    def __init__(self, message: str, code: str = "invalid_operation"):
        """Set the human-readable message and protocol error code."""
        super().__init__(message)
        self.code = code


def now() -> str:
    """Return a timezone-aware UTC timestamp for durable records."""
    return datetime.now(timezone.utc).isoformat()


def uid(prefix: str) -> str:
    """Generate a filesystem-safe random identifier with the requested prefix."""
    return f"{prefix}-{uuid.uuid4().hex[:16]}"


def canonical(value: Any) -> str:
    """Serialize JSON deterministically for fingerprints and request deduplication."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: bytes | str) -> str:
    """Return the SHA-256 digest of bytes or UTF-8 text."""
    return hashlib.sha256(value.encode("utf-8") if isinstance(value, str) else value).hexdigest()


def atomic_bytes(path: Path, data: bytes) -> None:
    """Flush content to a sibling temporary file and atomically replace the destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_json(path: Path, value: Any) -> None:
    """Write readable UTF-8 JSON using atomic file replacement."""
    atomic_bytes(path, (json.dumps(value, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))


def load_json(path: Path, default: Any = None) -> Any:
    """Load a UTF-8 JSON document, returning the default only when absent."""
    return json.loads(path.read_text("utf-8")) if path.exists() else default


def safe_path(root: Path, relative: str) -> Path:
    """Resolve a confined source path and reject traversal, private state, or symlinks."""
    if not relative or "\\" in relative or ":" in relative or "\x00" in relative:
        raise PilotError(f"Invalid repository-relative path: {relative!r}", "unsafe_path")
    p = PurePosixPath(relative)
    if p.is_absolute() or any(
        x.casefold() in {"..", ".git", ".repopilot", ".repopilotv2", ".repopilotv3"} for x in p.parts
    ):
        raise PilotError(f"Path is outside the permitted source tree: {relative}", "unsafe_path")
    if any(part.endswith((".", " ")) or PureWindowsPath(part).is_reserved() for part in p.parts):
        raise PilotError("Paths cannot use Windows device names or trailing-dot/space aliases", "unsafe_path")
    current = root
    for part in p.parts:
        current = current / part
        if current.is_symlink():
            raise PilotError(f"Symlink paths are not permitted: {relative}", "unsafe_path")
    if not current.resolve().is_relative_to(root.resolve()):
        raise PilotError("Path escapes repository", "unsafe_path")
    return current


def identifier(value: str) -> str:
    """Validate a task, session, or generation identifier before using it as a path."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}", value) or value in {".", ".."}:
        raise PilotError("IDs must contain 1–100 letters, digits, dots, underscores, or hyphens")
    return value


def dotnet_env(executable: str | None = None) -> dict[str, str]:
    """Build a child environment that disables SDK telemetry and background workload updates."""
    env = dict(os.environ)
    env.update(
        {
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
            "DOTNET_NOLOGO": "1",
            "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
            "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_DISABLE": "1",
            "DOTNET_CLI_WORKLOAD_UPDATE_NOTIFY_INTERVAL_HOURS": "999999",
            "DOTNET_CLI_USE_MSBUILD_SERVER": "0",
            "NUGET_XMLDOC_MODE": "skip",
        }
    )
    if executable and Path(executable).is_file() and not executable.lower().endswith(".exe"):
        env["DOTNET_ROOT"] = str(Path(executable).resolve().parent)
    if executable and Path(executable).is_file():
        env["PATH"] = str(Path(executable).resolve().parent) + os.pathsep + env.get("PATH", "")
        env["DOTNET_HOST_PATH"] = str(Path(executable).resolve())
    return env


def run(argv: list[str], cwd: Path, timeout: int = 60, env: dict | None = None) -> dict:
    """Execute a local argument array without a shell and capture bounded-time process evidence."""
    start = time.monotonic()
    try:
        p = subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        return {
            "argv": argv,
            "exit_code": p.returncode,
            "stdout": p.stdout,
            "stderr": p.stderr,
            "duration_seconds": round(time.monotonic() - start, 3),
        }
    except FileNotFoundError:
        raise PilotError(f"Executable not found: {argv[0]}", "missing_executable") from None
    except subprocess.TimeoutExpired as exc:
        return {
            "argv": argv,
            "exit_code": None,
            "stdout": str(exc.stdout or ""),
            "stderr": str(exc.stderr or ""),
            "timed_out": True,
            "duration_seconds": round(time.monotonic() - start, 3),
        }


def git(root: Path, *args: str) -> str:
    """Read Git output without interpreting commands through a shell."""
    result = run(["git", "-C", str(root), *args], root)
    return result["stdout"] if result["exit_code"] == 0 else ""


def discover_dotnet() -> str | None:
    """Find an explicitly configured, PATH, or user-space SDK executable."""
    candidates = [
        os.environ.get("REPOPILOTV3_DOTNET"),
        shutil.which("dotnet"),
        str(Path.home() / ".dotnet" / ("dotnet.exe" if os.name == "nt" else "dotnet")),
        str(Path.home() / "AppData/Local/Microsoft/dotnet/dotnet.exe"),
    ]
    return next((p for p in candidates if p and Path(p).is_file()), None)


def map_root(root: Path, mappings: list[dict]) -> str:
    """Map a host root to an explicitly configured worker-visible root."""
    local = str(root.resolve())
    for entry in sorted(mappings, key=lambda x: len(x["local"]), reverse=True):
        base = str(Path(entry["local"]).resolve())
        if local == base or local.startswith(base + os.sep):
            suffix = local[len(base) :].replace("\\", "/").lstrip("/")
            return entry["worker"].rstrip("/\\") + ("/" + suffix if suffix else "")
    return local


DEFAULTS = {
    "schema_version": "1.0",
    "dotnet": None,
    "worker_command": [],
    "path_mappings": [],
    "interop_paths": [],
    "reference_assembly_paths": [],
    "max_file_bytes": 1_000_000,
    "exclude": [],
    "profiles": {},
    "default_validation": None,
    "analysis": {"configuration": "Debug", "platform": "AnyCPU"},
    "response_tokens": 4000,
    "watch_seconds": 1.5,
    "export_cleanup": {"enabled": True, "max_bytes": 536870912, "target_bytes": 402653184,
                       "keep_generations": 2, "interval_seconds": 60},
}


def validate_settings(config: dict) -> None:
    """Reject invalid local configuration before dispatching trusted processes."""
    from jsonschema import Draft202012Validator

    schema = {
        "type": "object",
        "required": list(DEFAULTS),
        "properties": {
            "schema_version": {"const": "1.0"},
            "dotnet": {"type": ["string", "null"]},
            "worker_command": {"type": "array", "items": {"type": "string"}},
            "profiles": {
                "type": "object",
                "additionalProperties": {
                    "type": "object",
                    "required": ["kind", "timeout"],
                    "properties": {
                        "kind": {"enum": ["build", "test", "python_test", "check"]},
                        "argv": {"type": "array", "minItems": 1, "items": {"type": "string"}},
                        "args": {"type": "array", "items": {"type": "string"}},
                        "timeout": {"type": "integer", "minimum": 1, "maximum": 3600},
                        "runner": {"enum": ["auto", "VSTest", "Microsoft.Testing.Platform"]},
                    },
                    "additionalProperties": False,
                },
            },
            "default_validation": {"type": ["string", "null"]},
            "max_file_bytes": {"type": "integer", "minimum": 1},
            "response_tokens": {"type": "integer", "minimum": 500, "maximum": 100000},
            "watch_seconds": {"type": "number", "minimum": 0.2, "maximum": 60},
            "export_cleanup": {
                "type": "object", "required": list(DEFAULTS["export_cleanup"]),
                "properties": {"enabled": {"type": "boolean"},
                               "max_bytes": {"type": "integer", "minimum": 1},
                               "target_bytes": {"type": "integer", "minimum": 0},
                               "keep_generations": {"type": "integer", "minimum": 1, "maximum": 100},
                               "interval_seconds": {"type": "number", "minimum": 0, "maximum": 86400}},
                "additionalProperties": False,
            },
            **{
                key: {"type": "array", "items": {"type": "string"}}
                for key in ["interop_paths", "reference_assembly_paths", "exclude"]
            },
            "path_mappings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "required": ["local", "worker"],
                    "properties": {"local": {"type": "string"}, "worker": {"type": "string"}},
                    "additionalProperties": False,
                },
            },
            "analysis": {
                "type": "object",
                "properties": {
                    key: {"type": "string"} for key in ["configuration", "platform", "target_framework"]
                },
                "additionalProperties": False,
            },
        },
    }
    errors = list(Draft202012Validator(schema).iter_errors(config))
    if errors:
        raise PilotError(f"Invalid configuration: {errors[0].message}", "invalid_configuration")
    selected = config.get("default_validation")
    if selected and selected not in config["profiles"]:
        raise PilotError("default_validation must name a configured profile", "invalid_configuration")
    if config["export_cleanup"]["target_bytes"] > config["export_cleanup"]["max_bytes"]:
        raise PilotError("export_cleanup target_bytes must not exceed max_bytes", "invalid_configuration")


class Workspace:
    """Own one repository configuration, SQLite store, and host-specific private state."""

    def __init__(self, root: Path | str, state: Path | str | None = None):
        """Bind source and optional external state roots without modifying them."""
        self.root = Path(root).expanduser().resolve()
        self.state = Path(state).expanduser().resolve() if state else self.root / ".repopilotv3"
        self.config_path = self.state / "config.json"
        self.db_path = self.state / "index.sqlite3"

    @property
    def config(self) -> dict:
        """Load initialized configuration and enforce repository and host ownership."""
        if not self.config_path.exists():
            raise PilotError("Workspace is not initialized. Run repopilotv3 setup first.", "not_initialized")
        config = load_json(self.config_path)
        if not isinstance(config, dict):
            raise PilotError("Configuration must be an object", "invalid_configuration")
        cleanup = config.get("export_cleanup", {})
        if isinstance(cleanup, dict):
            config["export_cleanup"] = {**DEFAULTS["export_cleanup"], **cleanup}
        validate_settings(config)
        if config.get("root") != str(self.root):
            raise PilotError("State directory belongs to another repository", "wrong_repository")
        owner = config.get("owner", {})
        if owner != {"host": platform.node(), "system": platform.system()}:
            raise PilotError("State belongs to another host/OS; initialize separate state", "wrong_owner")
        return config

    def initialize(self) -> dict:
        """Create private state, storage tables, and local Git exclusions idempotently."""
        if not self.root.is_dir():
            raise PilotError("Repository root must be an existing directory")
        if self.config_path.exists():
            return self.config
        if self.state.is_symlink():
            raise PilotError("State directory cannot be a symlink")
        self.state.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            self.state.chmod(0o700)
        config = json.loads(canonical(DEFAULTS))
        config.update(
            root=str(self.root),
            dotnet=discover_dotnet(),
            owner={"host": platform.node(), "system": platform.system()},
            created_at=now(),
        )
        atomic_json(self.config_path, config)
        self._exclude_state()
        with self.db() as db:
            db.executescript(SCHEMA)
        return config

    def _exclude_state(self) -> None:
        """Add private state to Git local exclusions without editing tracked ignore rules."""
        if not self.state.is_relative_to(self.root):
            return
        exclude = git(self.root, "rev-parse", "--git-path", "info/exclude").strip()
        if not exclude:
            return
        path = Path(exclude)
        if not path.is_absolute():
            path = self.root / path
        git_root = git(self.root, "rev-parse", "--show-toplevel").strip()
        if not git_root or not self.state.is_relative_to(Path(git_root).resolve()):
            return
        line = "/" + self.state.relative_to(Path(git_root).resolve()).as_posix() + "/"
        previous = path.read_text("utf-8") if path.exists() else ""
        if line not in previous.splitlines():
            atomic_bytes(path, (previous.rstrip() + "\n" + line + "\n").encode())

    @contextlib.contextmanager
    def db(self, timeout: float = 30):
        """Open a transactional SQLite connection with foreign keys and serialized writes."""
        db = sqlite3.connect(self.db_path, timeout=timeout)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA journal_mode=WAL")
        try:
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @contextlib.contextmanager
    def lock(self, timeout: float = 30, scope: str | None = None):
        """Acquire the repository writer or an independent hashed operation scope."""
        self.config
        deadline = time.monotonic() + timeout
        name = "writer.lock" if scope is None else "operation-" + digest(scope) + ".lock"
        with (self.state / name).open("a+b") as f:
            if os.name == "nt":
                import msvcrt

                f.seek(0, os.SEEK_END)
                if not f.tell():
                    f.write(b"0")
                    f.flush()
            else:
                import fcntl

            while True:
                try:
                    if os.name == "nt":
                        f.seek(0)
                        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError as exc:
                    import errno

                    if exc.errno not in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                        raise
                    if time.monotonic() >= deadline:
                        raise PilotError(
                            "Repository is busy with another RepoPilot operation. Wait for it to finish; "
                            "if an older editor is still open, close its RepoPilot panel or reload that window. "
                            "The requested operation did not acquire the writer lock.", "busy"
                        ) from None
                    time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            try:
                yield
            finally:
                if os.name == "nt":
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(f, fcntl.LOCK_UN)

    def put(self, kind: str, key: str, value: dict) -> None:
        """Atomically insert or replace one structured record."""
        with self.db() as db:
            db.execute(
                "INSERT INTO records VALUES(?,?,?) ON CONFLICT(kind,id) DO UPDATE SET value=excluded.value",
                (kind, key, canonical(value)),
            )

    def get(self, kind: str, key: str) -> dict:
        """Read one structured record, raising not_found when absent."""
        with self.db() as db:
            row = db.execute("SELECT value FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        if row is None:
            raise PilotError(f"Unknown {kind}: {key}", "not_found")
        return json.loads(row[0])

    def records(self, kind: str) -> list[dict]:
        """Read all records of a kind in deterministic identifier order."""
        with self.db() as db:
            return [
                json.loads(r[0])
                for r in db.execute("SELECT value FROM records WHERE kind=? ORDER BY id", (kind,))
            ]


SCHEMA = """
CREATE TABLE IF NOT EXISTS records(kind TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(kind,id));
CREATE TABLE IF NOT EXISTS snapshots(id TEXT PRIMARY KEY, metadata TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS contents(hash TEXT PRIMARY KEY, content BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS search_documents(path TEXT, hash TEXT, PRIMARY KEY(path,hash));
CREATE TABLE IF NOT EXISTS files(snapshot TEXT, path TEXT, hash TEXT, content BLOB,
 PRIMARY KEY(snapshot,path), FOREIGN KEY(snapshot) REFERENCES snapshots(id));
CREATE VIRTUAL TABLE IF NOT EXISTS search USING fts5(path,body,snapshot UNINDEXED);
CREATE TABLE IF NOT EXISTS requests(session TEXT, id TEXT, hash TEXT, response TEXT,
 PRIMARY KEY(session,id));
CREATE TABLE IF NOT EXISTS events(session TEXT, id TEXT, value TEXT NOT NULL, PRIMARY KEY(session,id));
"""
