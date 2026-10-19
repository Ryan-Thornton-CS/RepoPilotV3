"""Regression coverage for competing writers and local archive/delete administration."""

from __future__ import annotations

import io
import json
import os
import queue
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from repopilotv3.core import PilotError, Workspace, atomic_json, run
from repopilotv3.manual.sessions import deletion_path
from repopilotv3.repository.index import RepositoryIndex, paths
from repopilotv3.rpc.server import BridgeServer
from repopilotv3.workflow.service import WorkflowService
from test_rpc_sessions import frame


def test_foreground_waits_and_background_defers_without_chat_errors(ws, session):
    """A competing writer delays startup, while periodic refresh stays silent and nonblocking."""
    server = BridgeServer(Workspace(ws.root), io.StringIO())
    entered = threading.Event()

    def prepare():
        """Signal a foreground request before it waits on the other connection's lock."""
        entered.set()
        return server.call("exports.prepare", {"session_id": session})

    try:
        with ThreadPoolExecutor(max_workers=1) as executor:
            with ws.lock():
                server.respond(frame(ws, "exports.prepare"), "exports.prepare", {"session_id": session, "background": True})
                response = [json.loads(line) for line in server.output.getvalue().splitlines() if '"result"' in line][-1]
                assert response["result"] == {"deferred": True, "reason": "writer_busy"}
                assert not server.service.history.page(session)
                pending = executor.submit(prepare)
                assert entered.wait(2)
                assert not pending.done()
            assert "manifest" in pending.result(timeout=20)
        with ws.lock():
            with pytest.raises(PilotError, match="did not acquire") as error:
                with Workspace(ws.root).lock(timeout=0):
                    pytest.fail("Concurrent writers must never enter together")
            assert error.value.code == "busy"
        assert not any(e["body"].get("action") == "operation_failed" for e in server.service.history.page(session))
    finally:
        server.executor.shutdown()


def test_delete_removes_chat_but_preserves_continuation_task_and_evidence(ws):
    """Deleted transcripts/drafts stay gone after restart; related chats and proof remain usable."""
    service = WorkflowService(ws)
    first = service.create("Remove me", objective="Shared ongoing work")["session"]
    other = service.create("Keep me", task_id=first["task_id"])["session"]
    sid = first["id"]
    service.history.preference("draft:" + sid, "Private draft")
    service.history.preference("draft:" + other["id"], "Keep this draft")
    service.history.preference("reply-output:" + sid + ":one", "Old response")
    service.history.preference("starter:" + sid, "Old starter")
    service.history.preference("context-advised:" + sid + ":40000", True)
    service.sessions.record(sid, "upload", "upload", "Uploaded text")
    package = service.prepare(sid)
    ws.put("validation", "local-evidence", {"id": "local-evidence", "task_id": first["task_id"]})
    ws.put("reply", sid + ":old-reply", {"session_id": sid})
    with ws.db() as db:
        db.execute("INSERT INTO requests VALUES(?,?,?,?)", (sid, "done", "hash", "{}"))
    service.select(sid)
    deleted = service.delete(sid)
    assert deleted["deleted"] and deleted["task_retained"] == first["task_id"]
    restored = WorkflowService(Workspace(ws.root))
    with pytest.raises(PilotError) as error:
        restored.status(sid)
    assert error.value.code == "session_deleted"
    assert restored.history.page(sid) == []
    assert restored.history.preference("draft:" + sid) is None
    assert restored.history.preference("starter:" + sid) is None
    assert restored.history.preference("reply-output:" + sid + ":one") is None
    assert restored.history.preference("context-advised:" + sid + ":40000") is None
    assert restored.history.preference("selected_session") is None
    assert restored.status(other["id"])["draft"] == "Keep this draft"
    assert restored.status(other["id"])["task"]["id"] == first["task_id"]
    assert len(ws.records("validation")) == 1
    assert ws.records("reply") == []
    assert [s["id"] for s in restored.sessions.list()] == [other["id"]]
    assert not (ws.state / "exports" / f"current-{sid}.json").exists()
    assert Path(package["directory"]).exists()
    with ws.db() as db:
        assert not db.execute("SELECT 1 FROM requests WHERE session=?", (sid,)).fetchone()
        assert not db.execute("SELECT 1 FROM events WHERE session=?", (sid,)).fetchone()


def test_delete_preserves_recovery_and_blocks_active_commands(ws):
    """Chats cannot be removed while commands run or patch journals need recovery."""
    service = WorkflowService(ws)
    sid = service.create()["session"]["id"]
    ws.put("command", "command-one", {"command_id": "command-one", "session_id": sid, "status": "running"})
    with pytest.raises(PilotError, match="running command"):
        service.delete(sid)
    ws.put("command", "command-one", {"command_id": "command-one", "session_id": sid, "status": "completed"})
    ws.put("patch", "patch-one", {"id": "patch-one", "session_id": sid})
    journal = ws.state / "patches/patch-one/recovery/journal.json"
    atomic_json(journal, {"state": "recovery_required"})
    with pytest.raises(PilotError, match="Recover"):
        service.delete(sid)
    assert service.sessions.get(sid)
    assert journal.exists()


def test_archive_restore_and_delete_are_local_administration(ws):
    """Archive hides a chat reversibly; delete is unavailable to the model tool registry."""
    server = BridgeServer(ws, io.StringIO())
    try:
        sid = server.call("sessions.create", {"name": "Archive me"})["session"]["id"]
        server.call("sessions.update", {"session_id": sid, "archived": True})
        assert not server.call("sessions.list", {})
        assert server.call("sessions.list", {"archived": True})[0]["id"] == sid
        server.call("sessions.update", {"session_id": sid, "archived": False})
        assert server.call("sessions.list", {})[0]["id"] == sid
        method, params = server.validate(frame(ws, "sessions.delete", {"session_id": sid}))
        assert server.call(method, params)["deleted"]
    finally:
        server.executor.shutdown()


def test_workspace_scan_excludes_editor_and_user_dependency_stores(ws):
    """Opening a parent folder cannot sweep editor extensions, credentials or package caches."""
    for directory in [".vscode-server", ".vscode-test", ".cache", ".codex", ".local", ".nuget", ".ssh", ".config", ".rustup"]:
        target = ws.root / directory / "nested/source.py"
        target.parent.mkdir(parents=True)
        target.write_text('"""Private cache fixture."""\n', encoding="utf-8")
    indexed = paths(ws)
    assert "greeter.py" in indexed
    assert not any("nested/source.py" in p for p in indexed)


def test_delete_rpc_bypasses_a_running_background_export(ws):
    """Deletion and history/status remain responsive while the repository worker is occupied."""
    server = BridgeServer(ws, io.StringIO())
    sid = server.call("sessions.create", {})["session"]["id"]
    other = server.call("sessions.create", {})["session"]["id"]
    entered, release = threading.Event(), threading.Event()
    messages = queue.Queue()
    server.emit = messages.put

    def busy_export(**kwargs):
        """Simulate a long source refresh that owns the repository writer."""
        with ws.lock():
            entered.set()
            assert release.wait(10)
        return {"done": True}

    server.service.prepare = busy_export
    read_fd, write_fd = os.pipe()
    reader = os.fdopen(read_fd, "r", encoding="utf-8")
    writer = os.fdopen(write_fd, "w", encoding="utf-8")
    thread = threading.Thread(target=server.serve, args=(reader,))
    thread.start()
    try:
        writer.write(json.dumps(frame(ws, "exports.prepare", {"session_id": sid, "background": True}, 1)) + "\n")
        writer.flush()
        assert entered.wait(2)
        writer.write(json.dumps(frame(ws, "sessions.delete", {"session_id": sid}, 2)) + "\n")
        writer.write(json.dumps(frame(ws, "sessions.status", {"session_id": other}, 3)) + "\n")
        writer.flush()
        responses = {}
        while not {2, 3}.issubset(responses):
            message = messages.get(timeout=2)
            if "id" in message:
                responses[message["id"]] = message
        assert responses[2]["result"]["deleted"]
        assert responses[3]["result"]["session"]["id"] == other
        assert not release.is_set()
        assert not server.service.sessions.list(query=sid)
    finally:
        release.set()
        writer.close()
        thread.join(5)
        reader.close()
        assert not thread.is_alive()


def test_delete_survives_index_write_lock_and_finishes_cleanup_after_restart(ws):
    """A durable marker hides a chat even when index SQLite is mid-transaction."""
    service = WorkflowService(ws)
    sid = service.create()["session"]["id"]
    service.history.preference("draft:" + sid, "Remove this draft")
    with ws.db() as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("INSERT INTO records VALUES('test','busy','{}')")
        result = service.delete(sid)
        assert result["deleted"] and result["cleanup_pending"]
        assert deletion_path(ws, sid).exists()
        assert not service.sessions.list(query=sid)
        with pytest.raises(PilotError) as error:
            service.sessions.get(sid)
        assert error.value.code == "session_deleted"
        assert service.history.preference("draft:" + sid) is None
    restored = WorkflowService(Workspace(ws.root))
    assert not json.loads(deletion_path(ws, sid).read_text())["cleanup_pending"]
    with ws.db() as db:
        assert not db.execute("SELECT 1 FROM records WHERE kind='session' AND id=?", (sid,)).fetchone()
    assert restored.delete(sid)["deleted"]


def test_active_session_lease_blocks_delete_but_not_another_chat(ws):
    """A session operation cannot race deletion; leases are independent and nest safely."""
    service = WorkflowService(ws)
    sid = service.create()["session"]["id"]
    other = service.create()["session"]["id"]
    with service.sessions.activity(sid):
        service.sessions.update(sid, name="Nested edit")
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(service.delete, sid)
            with pytest.raises(PilotError) as error:
                pending.result(timeout=2)
            assert error.value.code == "session_running"
            assert executor.submit(service.delete, other).result(timeout=2)["deleted"]
    assert service.delete(sid)["deleted"]


def test_status_reads_snapshot_identity_without_parsing_analysis(ws, monkeypatch):
    """Large historical snapshots cannot stall the fast session/status RPC path."""
    service = WorkflowService(ws)
    sid = service.create()["session"]["id"]
    package = service.prepare(sid)

    def forbidden(*args, **kwargs):
        """Detect an accidental full analysis load on the lightweight status path."""
        raise AssertionError("Status must not parse snapshot metadata")

    monkeypatch.setattr(RepositoryIndex, "current", forbidden)
    assert not service.status(sid)["package_stale"]
    assert RepositoryIndex(ws).current_id() == package["manifest"]["snapshot"]


def test_parent_folder_scan_honors_nested_ignores_with_one_config_read(ws, monkeypatch):
    """Nested generated runtimes stay excluded when the opened workspace is their parent."""
    nested = ws.root / "nested project Ω"
    nested.mkdir()
    assert run(["git", "init", "-q"], nested)["exit_code"] == 0
    (nested / ".gitignore").write_text("extension/backend/\n", encoding="utf-8")
    (nested / "code.py").write_text('"""Eligible source."""\n', encoding="utf-8")
    generated = nested / "extension/backend/runtime/generated.py"
    generated.parent.mkdir(parents=True)
    generated.write_text('"""Ignored bundled dependency."""\n', encoding="utf-8")
    reads = []
    original = Workspace.config.fget

    def config(instance):
        """Count configuration reads without changing the loaded inclusion rules."""
        reads.append(1)
        return original(instance)

    monkeypatch.setattr(Workspace, "config", property(config))
    included = paths(ws)
    assert "nested project Ω/code.py" in included
    assert not any("/extension/backend/" in name for name in included)
    assert len(reads) == 1
