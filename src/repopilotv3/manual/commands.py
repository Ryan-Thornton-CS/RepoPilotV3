"""Permission-bound command execution, durable logs, source diffs and process-tree cleanup."""

from __future__ import annotations

import codecs
import json
import os
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from repopilotv3.core import PilotError, atomic_bytes, atomic_json, dotnet_env, now, safe_path, uid
from repopilotv3.manual.sessions import SessionStore
from repopilotv3.repository.index import RepositoryIndex

WINDOWS_JOBS = []


def supervise_windows_tree() -> None:
    """Put the supervisor itself in a kill-on-close job before it creates any descendants."""
    if os.name != "nt":
        return
    import win32api
    import win32job

    job = win32job.CreateJobObject(None, "RepoPilotV3-" + uid("job"))
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    win32job.AssignProcessToJobObject(job, win32api.GetCurrentProcess())
    WINDOWS_JOBS.append(job)


class CommandRunner:
    """Execute explicitly authorized noninteractive commands and retain source observations."""

    active: dict[tuple[str, str], tuple[subprocess.Popen, threading.Event]] = {}
    mutex = threading.Lock()
    closing_states: set[str] = set()

    def __init__(self, ws, notify=None):
        """Use shared workspace state and an optional bounded live-progress callback."""
        self.ws = ws
        self.notify = notify or (lambda value: None)

    def get(self, session_id: str, command_id: str) -> dict:
        """Prevent command logs and diff documents from crossing chat boundaries."""
        record = self.ws.get("command", command_id)
        if record["session_id"] != session_id:
            raise PilotError("Command belongs to another session", "wrong_session")
        execution = self.ws.state / "commands" / command_id / "execution.json"
        if record["status"] == "running" and execution.exists():
            record = {**record, **json.loads(execution.read_text("utf-8")), "source_observation_incomplete": True}
        return record

    def resolve(self, argv: list[str]) -> list[str]:
        """Select configured SDK/bundled Python before falling back to the host PATH."""
        result = list(argv)
        if any("\0" in arg for arg in result):
            raise PilotError("Command arguments cannot contain NUL bytes", "invalid_command")
        if result[0] in {"python", "python3", "python.exe"}:
            result[0] = sys.executable
        elif result[0] in {"dotnet", "dotnet.exe"} and self.ws.config.get("dotnet"):
            result[0] = self.ws.config["dotnet"]
        executable = shutil.which(result[0])
        if not executable:
            raise PilotError(f"Executable unavailable: {result[0]}. Configure its path or install it locally.",
                             "missing_executable")
        result[0] = executable
        return result

    def request(self, session_id: str, arguments: dict) -> dict:
        """Stage a command when permission is missing; otherwise execute it exactly once."""
        session = SessionStore(self.ws).guard(session_id, "run_command")
        cwd = safe_path(self.ws.root, arguments.get("cwd", "."))
        if not cwd.is_dir():
            raise PilotError("Command working directory must be an existing repository directory", "invalid_cwd")
        argv = self.resolve(arguments["argv"])
        record = {"command_id": uid("command"), "session_id": session_id, "task_id": session["task_id"],
                  "argv": argv, "cwd": str(cwd), "timeout": arguments.get("timeout", 300),
                  "status": "awaiting_permission", "created_at": now()}
        self.ws.put("command", record["command_id"], record)
        if not session["commands_allowed"]:
            return {**record, "pending_permission": True,
                    "next": "The developer must grant command permission locally; the model cannot authorize it."}
        return self.execute(session_id, record["command_id"])

    def execute(self, session_id: str, command_id: str) -> dict:
        """Run one approved command; persisted running/terminal records are never replayed."""
        record = self.get(session_id, command_id)
        if record["status"] != "awaiting_permission":
            raise PilotError("Command is not pending. Inspect its saved result; do not replay it.", "command_conflict")
        if not SessionStore(self.ws).get(session_id)["commands_allowed"]:
            raise PilotError("Grant session command permission locally first", "permission_required")
        with self.mutex:
            if str(self.ws.state) in self.closing_states:
                raise PilotError("Backend connection closed before execution; command was not started", "command_interrupted")
        index = RepositoryIndex(self.ws)
        before = index.refresh()
        before_files = index.files(before["id"])
        base = self.ws.state / "commands" / command_id
        base.mkdir(parents=True, exist_ok=True)
        record.update(status="running", started_at=now(), before_snapshot=before["id"])
        self.ws.put("command", command_id, record)
        atomic_json(base / "launch.json", {**record, "host_result": str(base / "execution.json")})
        process = subprocess.Popen(
            [sys.executable, "-I", "-B", "-u", "-m", __name__, "--host", str(base / "launch.json")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            env={**os.environ, **dotnet_env(self.ws.config.get("dotnet")), "PYTHONUTF8": "1"},
            cwd=self.ws.root, bufsize=0,
        )
        cancelled = threading.Event()
        key = (str(self.ws.state), command_id)
        with self.mutex:
            self.active[key] = (process, cancelled)
            if str(self.ws.state) in self.closing_states:
                process.stdin.close()
        chunks = queue.Queue(maxsize=64)

        def read_output():
            """Drain bytes without blocking status requests or retaining an unbounded in-memory log."""
            try:
                while data := process.stdout.read(4096):
                    chunks.put(data)
            finally:
                chunks.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        tail, last_emit = "", 0.0
        self.notify({"session_id": session_id, "command_id": command_id, "state": "running"})
        try:
            with (base / "output.log").open("wb") as log:
                while True:
                    chunk = chunks.get()
                    if chunk is None:
                        break
                    log.write(chunk)
                    tail = (tail + decoder.decode(chunk))[-8000:]
                    if time.monotonic() - last_emit > .2:
                        self.notify({"session_id": session_id, "command_id": command_id,
                                     "state": "running", "text": tail[-3000:]})
                        last_emit = time.monotonic()
                log.flush()
            process.wait(timeout=10)
        finally:
            if process.poll() is None:
                process.stdin.close()
                process.wait(timeout=10)
            else:
                process.stdin.close()
            process.stdout.close()
            reader.join(timeout=2)
            with self.mutex:
                self.active.pop(key, None)
        execution_path = base / "execution.json"
        execution = json.loads(execution_path.read_text("utf-8")) if execution_path.exists() else {
            "status": "interrupted", "exit_code": process.returncode}
        record.update(**execution, finished_at=now(), output=tail, log_file="output.log")
        try:
            after = index.refresh()
        except (PilotError, OSError) as exc:
            record.update(files=[], snapshot=before["id"], source_observation_incomplete=True,
                          observation_error=str(exc), evidence_kind="command",
                          next="The command already executed. Do not rerun it to repair this observation error; refresh source/context and inspect the saved command log.")
            self.ws.put("command", command_id, record)
            self.notify({"session_id": session_id, "command_id": command_id, "state": record["status"]})
            return record
        after_files = index.files(after["id"])
        files = []
        for path in sorted(set(before_files) | set(after_files)):
            old, new = before_files.get(path), after_files.get(path)
            if old != new:
                if old is not None:
                    atomic_bytes(base / "before" / path, old)
                if new is not None:
                    atomic_bytes(base / "after" / path, new)
                files.append({"path": path, "status": "added" if old is None else "deleted" if new is None else "modified"})
        record.update(after_snapshot=after["id"], snapshot=after["id"], files=files,
                      coverage="Eligible source changes observed during execution; concurrent edits may contribute.",
                      evidence_kind="command", next="Use command_output for bounded log expansion; use validate for test evidence.")
        self.ws.put("command", command_id, record)
        self.notify({"session_id": session_id, "command_id": command_id, "state": record["status"]})
        return record

    def cancel(self, session_id: str, command_id: str) -> dict:
        """Signal the supervisor without acquiring the repository writer lock."""
        self.get(session_id, command_id)
        with self.mutex:
            running = self.active.get((str(self.ws.state), command_id))
            if running:
                running[1].set()
                try:
                    running[0].stdin.close()
                except OSError:
                    pass
        return {"command_id": command_id, "cancellation_requested": bool(running)}

    def cancel_all(self) -> None:
        """Close supervisor pipes when the backend disconnects, terminating managed process trees."""
        with self.mutex:
            self.closing_states.add(str(self.ws.state))
            for (state, _), (process, cancelled) in list(self.active.items()):
                if state == str(self.ws.state):
                    cancelled.set()
                    try:
                        process.stdin.close()
                    except OSError:
                        pass

    def output(self, session_id: str, command_id: str, start: int = 1, end: int = 80) -> dict:
        """Read at most 500 lines/12,000 characters without exposing another command's files."""
        record = self.get(session_id, command_id)
        first, last = max(1, start), min(max(start, end), max(1, start) + 499)
        path = self.ws.state / "commands" / command_id / "output.log"
        selected, size, total = [], 0, 0
        if path.exists():
            with path.open(encoding="utf-8", errors="replace") as stream:
                for number, line in enumerate(stream, 1):
                    total = number
                    if first <= number <= last and size < 12000:
                        selected.append(line[:12000 - size])
                        size += len(selected[-1])
        return {"command_id": command_id, "status": record["status"], "start": first,
                "end": first + len(selected) - 1, "text": "".join(selected), "total_lines": total,
                "omitted": total > last or size >= 12000}

    def diffs(self, session_id: str, command_id: str, path: str | None = None) -> dict:
        """Return immutable original/current observations for native read-only diff documents."""
        record = self.get(session_id, command_id)
        files = []
        for item in record.get("files", []):
            if path is not None and item["path"] != path:
                continue
            expanded = dict(item)
            if path is not None:
                base = self.ws.state / "commands" / command_id
                for field, directory in [("original", "before"), ("proposed", "after")]:
                    source = base / directory / item["path"]
                    expanded[field] = source.read_bytes().decode("utf-8") if source.exists() else ""
            files.append(expanded)
        if path is not None and not files:
            raise PilotError("File is not in this command's source observations", "not_found")
        return {"command_id": command_id, "files": files, "session_id": session_id}


def host(config_path: Path) -> int:
    """Supervise a child tree; stdin EOF detects even abrupt loss of the Python backend."""
    config = json.loads(config_path.read_text("utf-8"))
    stop = threading.Event()

    def watch_parent():
        """Wait on the backend-owned pipe, independent of child output and command duration."""
        while os.read(sys.stdin.fileno(), 4096):
            pass
        stop.set()

    threading.Thread(target=watch_parent, daemon=True).start()
    supervise_windows_tree()
    process = subprocess.Popen(config["argv"], cwd=config["cwd"], stdin=subprocess.DEVNULL,
                               start_new_session=os.name != "nt",
                               creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0)
    status, began = "completed", time.monotonic()
    try:
        while process.poll() is None:
            if stop.wait(.05) or time.monotonic() - began >= config["timeout"]:
                status = "cancelled" if stop.is_set() else "timed_out"
                break
    finally:
        # Also clean up background descendants when the root command exits successfully.
        if os.name == "nt":
            if process.poll() is None:
                subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                               capture_output=True, timeout=10, check=False)
        else:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=10)
    atomic_json(Path(config["host_result"]), {"status": status if status != "completed" else
                "completed" if process.returncode == 0 else "failed", "exit_code": process.returncode})
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--host":
        raise SystemExit(host(Path(sys.argv[2])))
    raise SystemExit("This internal supervisor is started only by an authorized command request.")
