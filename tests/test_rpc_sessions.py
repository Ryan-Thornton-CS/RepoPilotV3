"""Persistent session/history contracts and the management protocol's trust boundaries."""

from __future__ import annotations

import io
import json
import threading
from pathlib import Path

import pytest

from conftest import request
from repopilotv3.core import PilotError, Workspace
from repopilotv3.manual.sessions import SessionStore
from repopilotv3.rpc.server import BridgeServer
from repopilotv3.tasks.store import TaskStore
from repopilotv3.workflow.service import WorkflowService
from test_workflow import proposal, start


def frame(ws, method, params=None, number=1):
    """Construct a concrete versioned, explicitly workspace-bound management frame."""
    return {
        "jsonrpc": "2.0",
        "id": number,
        "method": method,
        "params": {"workspace": str(ws.root), "bridge_version": "1.0", **(params or {})},
    }


def test_independent_sessions_drafts_history_and_passive_restore(ws):
    """Selection, names, drafts and transcripts survive restarts without executing saved text."""
    service = WorkflowService(ws)
    first = service.create("First Ω", objective="First task")["session"]["id"]
    second = service.create("Second", objective="Second task")["session"]["id"]
    assert SessionStore(ws).get(first)["task_id"] != SessionStore(ws).get(second)["task_id"]
    service.history.preference("draft:" + first, request("validate", {"profile": "never-run"}))
    service.history.preference("draft:" + second, "Other draft")
    service.note(first, "Local note <script>never execute</script>")
    service.sessions.update(first, name="Renamed")
    restored = WorkflowService(Workspace(ws.root))
    assert restored.history.preference("selected_session") == second
    assert restored.status(first)["draft"].find("never-run") >= 0
    assert restored.status(second)["draft"] == "Other draft"
    assert not ws.records("validation")
    assert not restored.sessions.get(first)["todo_ready"]
    assert restored.sessions.list("renamed")[0]["id"] == first
    restored.sessions.update(first, archived=True)
    assert not restored.sessions.list("renamed")
    assert restored.sessions.list("renamed", archived=True)
    with pytest.raises(PilotError, match="archived"):
        restored.execute(first, request("todo", {}))
    restored.sessions.update(first, archived=False)
    assert restored.history.page(first)[-1]["kind"] == "note"


def test_history_deduplicates_and_pages_without_replaying(ws, session):
    """A repeated model request has one persistent request/result pair and one execution."""
    service = WorkflowService(ws)
    text = request("todo", {"objective": "Do once"})
    first = service.execute(session, text)
    assert service.execute(session, text) == first
    history = service.history.page(session)
    assert [r["kind"] for r in history] == ["request", "result"]
    assert len(TaskStore(ws).list()) == 1
    for i in range(12):
        service.note(session, f"Note {i}")
    page = service.history.page(session, limit=5)
    earlier = service.history.page(session, before=page[0]["sequence"], limit=5)
    assert page[0]["sequence"] > earlier[-1]["sequence"]


def test_handoff_identifiers_and_local_recovery_progress(ws, session):
    """Fresh chats share the task but retain separate bootstrap, TODO readiness and draft state."""
    task = start(ws, session)
    service = WorkflowService(ws)
    old = service.prepare(session)
    assert service.handoff(session)["requires_new_chat_confirmation"]
    new = service.handoff(session, True)
    next_id = new["session_id"]
    assert next_id != session and service.sessions.get(next_id)["task_id"] == task
    assert not service.sessions.get(next_id)["todo_ready"]
    bootstrap = (Path(new["directory"]) / "06_CONTINUATION.md").read_text()
    assert next_id in bootstrap and session not in bootstrap
    assert (
        new["manifest"]["artifacts"]["06_CONTINUATION.md"]["hash"]
        != old["manifest"]["artifacts"]["06_CONTINUATION.md"]["hash"]
    )
    service.progress(next_id, {"decisions": ["Recovered without previous model"]})
    assert not service.sessions.get(next_id)["todo_ready"]
    assert "Recovered without previous model" in TaskStore(ws).get(task)["decisions"]
    assert service.execute(next_id, request("todo", {}, "resume"))["ok"]


def test_unchanged_prepare_survives_service_restart_and_transfer_counts_once(ws, session):
    """Unchanged publication is reused across reconnects and copy/confirm count one transfer."""
    start(ws, session)
    service = WorkflowService(ws)
    first = service.prepare(session)
    second = WorkflowService(ws).prepare(session)
    assert second["unchanged"] and second["manifest"]["id"] == first["manifest"]["id"]
    name = "00_PROJECT_INSTRUCTIONS.md"
    text = (Path(first["directory"]) / name).read_text(encoding="utf-8")
    service.transfer(session, text)
    count = service.sessions.metrics(session)["events"]
    service.confirm(session, first["manifest"]["id"], {name: first["manifest"]["artifacts"][name]["hash"]})
    assert service.sessions.metrics(session)["events"] == count
    (ws.root / "greeter.py").write_text((ws.root / "greeter.py").read_text() + "\n# changed\n")
    updated = service.prepare(session)
    assert updated["manifest"]["snapshot"] != first["manifest"]["snapshot"]
    assert updated["manifest"]["artifacts"][name]["hash"] == first["manifest"]["artifacts"][name]["hash"]


def test_patch_admin_separate_and_review_scoped(ws, session):
    """Model tools cannot apply/administer proposals and another chat cannot review them."""
    start(ws, session)
    service = WorkflowService(ws)
    staged = service.execute(session, request("propose_patch", proposal(ws), "patch"))
    patch = staged["data"]["patch_id"]
    other = service.sessions.create()["id"]
    with pytest.raises(PilotError, match="different session"):
        service.review(other, patch)
    assert not service.execute(session, request("patch.approve", {"patch_id": patch}, "bad-admin"))["ok"]
    review = service.review(session, patch)
    assert "Hello, {name}!" in review["files"][0]["proposed"]
    rejected = service.approve(session, patch, False)
    assert rejected["data"]["status"] == "rejected"
    with pytest.raises(PilotError):
        service.approve(session, patch, True)
    assert "Hello {name}" in (ws.root / "greeter.py").read_text()


def test_rpc_frames_versions_scopes_and_unknown_methods(ws):
    """Management accepts only known closed methods bound to this workspace/protocol."""
    server = BridgeServer(ws, io.StringIO())
    assert server.validate(frame(ws, "hello")) == ("hello", {})
    for message in [
        frame(ws, "shell"),
        frame(ws, "hello", {"workspace": "/wrong"}),
        frame(ws, "hello", {"bridge_version": "2.0"}),
        frame(ws, "sessions.create", {"command": "arbitrary"}),
        [],
        {"jsonrpc": "2.0", "id": True, "method": "hello", "params": {}},
    ]:
        with pytest.raises(PilotError):
            server.validate(message)
    server.executor.shutdown()


def test_rpc_serves_history_while_repository_job_runs(ws, monkeypatch):
    """A slow repository job does not monopolize the stdio reader or history service."""
    output = io.StringIO()
    server = BridgeServer(ws, output)
    session = server.service.create(objective="Concurrent metadata")["session"]["id"]
    started, release = threading.Event(), threading.Event()
    original = server.call

    def call(method, params):
        """Delay one source action while allowing metadata calls to complete independently."""
        if method == "exports.prepare":
            started.set()
            assert release.wait(5)
            return {"completed": True}
        return original(method, params)

    monkeypatch.setattr(server, "call", call)
    thread = threading.Thread(
        target=server.respond, args=(frame(ws, "exports.prepare"), "exports.prepare", {"session_id": session})
    )
    thread.start()
    assert started.wait(3)
    server.respond(frame(ws, "sessions.status", number=2), "sessions.status", {"session_id": session})
    assert any(r.get("id") == 2 for r in map(json.loads, output.getvalue().splitlines()))
    release.set()
    thread.join(5)
    assert not thread.is_alive()
    server.executor.shutdown()


def test_rpc_malformed_input_and_uninitialized_setup(tmp_path):
    """Missing state is actionable; setup touches private files and no harness directory."""
    root = tmp_path / "Root Ω"
    root.mkdir()
    ws = Workspace(root)
    output = io.StringIO()
    server = BridgeServer(ws, output)
    lines = [
        "{bad json}",
        "[" * 1500 + "0" + "]" * 1500,
        json.dumps(frame(ws, "hello", number=2)),
        json.dumps(frame(ws, "sessions.list", number=3)),
        json.dumps(frame(ws, "setup", number=4)),
    ]
    server.serve(io.StringIO("\n".join(lines) + "\n"))
    results = {r["id"]: r for r in map(json.loads, output.getvalue().splitlines()) if "id" in r}
    assert "error" in results[None]
    assert not results[2]["result"]["initialized"]
    assert results[3]["error"]["data"]["code"] == "not_initialized"
    assert results[4]["result"]["harness_written"] is False
    assert not (root / ".github").exists() and not (root / ".mcp.json").exists()


def test_failed_session_creation_has_no_partial_records(ws):
    """Invalid display metadata is rejected before creating a task or orphan chat."""
    service = WorkflowService(ws)
    with pytest.raises(PilotError, match="Session names"):
        service.create("x" * 161, objective="Must not be created")
    assert not service.sessions.list() and not TaskStore(ws).list()
    first = service.create("x" * 160, objective="Continuation name boundary")["session"]["id"]
    next_ = service.handoff(first, True)
    assert len(service.sessions.get(next_["session_id"])["name"]) <= 160


def test_published_management_schema_matches_closed_runtime_methods(ws):
    """Published versioned schemas describe every management method without a shell escape."""
    from importlib.resources import files

    from jsonschema import Draft202012Validator

    from repopilotv3.rpc.server import METHODS

    schema = json.loads(files("repopilotv3.resources").joinpath("schemas/v1/bridge.schema.json").read_text())
    declared = {s["properties"]["method"]["const"] for s in schema["oneOf"]}
    assert declared == set(METHODS)
    validator = Draft202012Validator(schema)
    validator.validate(frame(ws, "hello"))
    assert list(validator.iter_errors(frame(ws, "shell")))
    assert list(validator.iter_errors(frame(ws, "hello", {"command": "bad"})))


def test_rpc_diff_lists_then_expands_one_complete_file_and_saves_failures(ws, session):
    """Native review fetches whole units on demand; management failures stay in history."""
    start(ws, session)
    service = WorkflowService(ws)
    staged = service.execute(session, request("propose_patch", proposal(ws), "patch"))
    patch = staged["data"]["patch_id"]
    output = io.StringIO()
    server = BridgeServer(ws, output)
    params = {"session_id": session, "patch_id": patch}
    listing = server.call("patch.review", params)
    assert "original" not in listing["files"][0]
    expanded = server.call("patch.review", {**params, "path": "greeter.py"})
    assert len(expanded["files"]) == 1
    assert "Hello {name}" in expanded["files"][0]["original"]
    assert "Hello, {name}!" in expanded["files"][0]["proposed"]
    server.respond(frame(ws, "patch.review"), "patch.review", {**params, "path": "missing.py"})
    failure = service.history.page(session)[-1]
    assert failure["body"]["action"] == "operation_failed"
    assert failure["body"]["error"]["code"] == "not_found"
    server.executor.shutdown()


def test_cli_redirected_output_uses_utf8_for_unicode_paths(ws, monkeypatch):
    """A Windows-style legacy output encoding cannot break JSON containing Unicode roots."""
    import sys

    from repopilotv3.cli import main

    buffer = io.BytesIO()
    output = io.TextIOWrapper(buffer, encoding="ascii")
    monkeypatch.setattr(sys, "stdout", output)
    assert main(["--repo", str(ws.root), "setup"]) == 0
    output.flush()
    assert "Ω" in buffer.getvalue().decode("utf-8")


def test_automatic_packages_preserve_strategy_and_continuation_first_message(ws, session):
    """Background-ready packages reuse artifacts and continue tasks without reusing permissions."""
    task_id = start(ws, session)
    service = WorkflowService(ws)
    service.configure(session, commands_allowed=True, context_limit=1000)
    service.history.preference("draft:" + session, "Keep original draft")
    metrics = service.status(session)["metrics"]
    first = service.prepare(session, full_source=True)
    assert service.status(session)["full_source"] is True
    again = service.prepare(session)
    assert again["unchanged"] and again["manifest"]["id"] == first["manifest"]["id"]
    assert service.status(session)["metrics"] == metrics
    continued = service.handoff(session, True)
    new_id = continued["session_id"]
    status = service.status(new_id)
    assert status["task"]["id"] == task_id
    assert not status["session"]["todo_ready"] and not status["session"]["commands_allowed"]
    assert status["session"]["context_limit"] == 1000
    harness = [entry for entry in service.history.page(new_id) if entry["kind"] == "harness"]
    assert harness[0]["body"]["action"] == "starter"
    assert harness[0]["body"]["copy_text"] == continued["copy_text"]
    assert service.history.preference("draft:" + session) == "Keep original draft"


def test_blank_chat_context_prepares_without_inventing_a_task(ws):
    """A fresh chat can publish orientation automatically without TODO or transfer side effects."""
    service = WorkflowService(ws)
    session_id = service.create()["session"]["id"]
    before = service.status(session_id)["metrics"]
    package = service.prepare(session_id)
    assert (Path(package["directory"]) / "01_REPOSITORY_MAP.md").is_file()
    assert service.status(session_id)["task"] is None
    assert service.status(session_id)["metrics"] == before
    assert not service.sessions.get(session_id)["todo_ready"]
