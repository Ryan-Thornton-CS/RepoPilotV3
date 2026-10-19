"""End-to-end manual operations and shared Copilot checkpoint continuation."""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import pytest

from repopilotv3.core import PilotError, atomic_json, digest
from repopilotv3.manual.bridge import ManualBridge
from repopilotv3.manual.sessions import SessionStore
from repopilotv3.manual.validation import ValidationEvidence, validate
from repopilotv3.repository.service import RepositoryTools
from repopilotv3.tasks.store import TaskStore
from repopilotv3.workflow.service import WorkflowService
from conftest import request


def start(ws, session):
    """Initialize the fixture's persistent task through the actual manual bridge."""
    result = ManualBridge(ws).execute(
        session,
        request(
            "todo",
            {
                "objective": "Fix greeting punctuation",
                "seeds": ["greeter.py"],
                "progress": {
                    "items": [
                        {"id": "fix", "text": "Fix greeting punctuation and verify", "status": "active"}
                    ]
                },
            },
            "start",
        ),
    )
    assert result["ok"], result
    return result["task_id"]


def proposal(ws):
    """Build an exact synthetic greeting patch from the current captured bytes."""
    snap = RepositoryTools(ws).snapshot()
    return {
        "base_snapshot": snap["id"],
        "base_hashes": {"greeter.py": snap["hashes"]["greeter.py"]},
        "diff": '--- a/greeter.py\n+++ b/greeter.py\n@@ -9 +9 @@\n-        return f"Hello {name}"\n+        return f"Hello, {name}!"\n',
    }


def test_complete_manual_flow(ws, session):
    """Execute TODO, reads, approval, real pytest, refreshed source, and continuation."""
    task_id = start(ws, session)
    config = ws.config
    config["profiles"]["tests"] = {
        "kind": "python_test",
        "timeout": 60,
        "args": [],
        "argv": [sys.executable, "-m", "pytest", "--junitxml={results_dir}/junit.xml"],
    }
    config["default_validation"] = "tests"
    atomic_json(ws.config_path, config)
    ctl = WorkflowService(ws)
    prepared = ctl.prepare(session, True)
    assert any(x.startswith("full/") for x in prepared["manifest"]["artifacts"])
    assert not any(x["action"] == "keep" for x in prepared["checklist"])
    names = ["00_PROJECT_INSTRUCTIONS.md", "01_REPOSITORY_MAP.md"]
    rows = ctl.confirm(
        session, prepared["manifest"]["id"], {n: prepared["manifest"]["artifacts"][n]["hash"] for n in names}
    )
    assert rows[0]["action"] == "keep"
    context = ctl.execute(session, request("context", {"paths": ["greeter.py"]}, "context"))
    assert context["ok"] and context["data"]["sources"]
    staged = ctl.execute(session, request("propose_patch", proposal(ws), "patch"))
    assert staged["ok"], staged
    patch_id = staged["data"]["patch_id"]
    assert "Hello {name}" in (ws.root / "greeter.py").read_text()
    review = ctl.review(session, patch_id)
    assert review["whole_proposal_only"] and review["files"][0]["proposed"]
    applied = ctl.approve(session, patch_id, True)
    assert applied["data"]["status"] == "applied"
    assert applied["data"]["validation"]["status"] == "passed", applied
    assert TaskStore(ws).get(task_id)["validation"]
    ctl.transfer(session, json.dumps(applied))
    ctl.transfer(session, json.dumps(applied))
    metrics = SessionStore(ws).metrics(session)
    assert metrics["confidence"] == "low" and not metrics["tracking_complete"]
    assert len(list((ws.state / "outbox" / session).glob("*.json"))) >= 3
    continued = ctl.handoff(session, True)
    new_session = continued["session_id"]
    assert new_session != session
    assert not SessionStore(ws).get(new_session)["todo_ready"]
    resumed = ctl.execute(new_session, request("todo", {"task_id": task_id}, "resume"))
    assert resumed["ok"]
    assert TaskStore(ws).get(task_id)["items"][0]["id"] == "fix"


def test_todo_guard_duplicate_import_and_id_conflict(ws, session):
    """Reject uninitialized reads and conflicting IDs without replaying effects."""
    bridge = ManualBridge(ws)
    blocked = bridge.execute(session, request("read", {"path": "greeter.py"}))
    assert blocked["error"]["code"] == "todo_required"
    task = start(ws, session)
    assert start(ws, session) == task
    assert len(TaskStore(ws).list()) == 1
    with pytest.raises(PilotError, match="reused"):
        bridge.execute(session, request("search", {"query": "other"}))
    assert not bridge.execute(session, request("validate", {"profile": "unknown"}, "validation"))["ok"]


def test_progress_preserves_requirements_and_local_facts(ws, session):
    """Model updates cannot delete requirements, change objectives, or claim passed builds."""
    task_id = start(ws, session)
    store = TaskStore(ws)
    store.update(task_id, {"items": [], "decisions": ["Keep the interface"]})
    assert len(store.get(task_id)["items"]) == 1
    with pytest.raises(PilotError):
        store.update(task_id, {"validation": ["passed"]})
    prior = store.get(task_id)["items"][0]["text"]
    store.update(task_id, {"items": [{"id": "fix", "text": "Refine the implementation step", "status": "completed"}]})
    assert any(h.get("item", {}).get("text") == prior for h in store.get(task_id)["history"])
    assert store.get(task_id)["items"][0]["verification"] == "unverified"
    assert store.get(task_id)["validation"] == []


def test_copilot_checkpoint_fallback_two_chats_and_recovery(ws):
    """Resume a Copilot task independently in two chats and recover developer progress."""
    saved = RepositoryTools(ws).checkpoint(objective="Fix greeting", progress={"decisions": ["Preserve API"]})
    task_id = saved["task"]["id"]
    first = SessionStore(ws).create(task_id, "same-project")["id"]
    second = SessionStore(ws).create(task_id, "same-project")["id"]
    ctl = WorkflowService(ws)
    ctl.execute(first, request("todo", {}, "resume"))
    assert not SessionStore(ws).get(second)["todo_ready"]
    prepared = ctl.prepare(first)
    assert (Path(prepared["directory"]) / "06_CONTINUATION.md").is_file()
    ctl.progress(first, {"decisions": ["Previous model unavailable; inspect code first"]})
    assert "Preserve API" in TaskStore(ws).get(task_id)["decisions"]


def test_real_tests_required_and_evidence_becomes_historical(ws):
    """Exit success alone cannot establish tests; changed profiles/source stale evidence."""
    config = ws.config
    config["profiles"]["fake-tests"] = {
        "kind": "python_test",
        "args": [],
        "timeout": 10,
        "argv": [sys.executable, "-c", "print('claimed pass')"],
    }
    config["profiles"]["check"] = {
        "kind": "check",
        "args": [],
        "timeout": 10,
        "argv": [sys.executable, "-c", "print('actual check')"],
    }
    atomic_json(ws.config_path, config)
    assert validate(ws, "fake-tests")["status"] == "inconclusive_no_test_evidence"
    record = validate(ws, "check")
    assert record["status"] == "passed" and record["evidence_kind"] == "check"
    assert not ValidationEvidence(ws).historical(record, RepositoryTools(ws).snapshot()["id"])
    (ws.root / "README.md").write_text("changed")
    assert ValidationEvidence(ws).historical(record, RepositoryTools(ws).snapshot()["id"])


def test_read_only_batch_and_no_runtime_network(ws, session, monkeypatch):
    """Process a bounded read batch without credentials or RepoPilot network access."""

    def denied(*args, **kwargs):
        """Fail if local workflow code attempts an outbound socket connection."""
        raise AssertionError("Unexpected network access")

    monkeypatch.setattr(socket.socket, "connect", denied)
    start(ws, session)
    result = ManualBridge(ws).execute(
        session,
        request(
            "batch",
            {
                "calls": [
                    {"tool": "search", "arguments": {"query": "greet"}},
                    {"tool": "read", "arguments": {"path": "greeter.py"}},
                ]
            },
            "batch",
        ),
    )
    assert result["ok"] and len(result["data"]["results"]) == 2
    assert "snapshot" in result["data"]["results"][0]["data"]


def test_interrupted_request_is_not_reexecuted(ws, session):
    """A reserved interrupted operation requires inspection instead of blind replay."""
    text = request("todo", {"objective": "Do not replay"})
    with ws.db() as db:
        db.execute(
            "INSERT INTO requests VALUES(?,?,?,NULL)",
            (
                session,
                "one",
                digest(
                    json.dumps(json.loads(text), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                ),
            ),
        )
    with pytest.raises(PilotError, match="interrupted"):
        ManualBridge(ws).execute(session, text)


def test_invalid_json_produces_copyable_saved_error(ws, session):
    """A malformed model answer still yields useful output for the next WebUI turn."""
    ctl = WorkflowService(ws)
    result = ctl.execute(session, "this is not JSON")
    assert not result["ok"] and result["error"]["code"] == "invalid_request"
    assert ctl.history.page(session)[-1]["body"]["error"]["code"] == "invalid_request"
    assert list((ws.state / "outbox" / session).glob("*.json"))


def test_validation_evidence_does_not_leak_between_tasks(ws):
    """A chat's continuation must not present another task's validation as its own."""
    config = ws.config
    config["profiles"]["check"] = {
        "kind": "check",
        "args": [],
        "timeout": 10,
        "argv": [sys.executable, "-c", "print('actual check')"],
    }
    atomic_json(ws.config_path, config)
    first = TaskStore(ws).create("First task")["id"]
    second = TaskStore(ws).create("Second task")["id"]
    evidence = validate(ws, "check", first)
    snapshot = RepositoryTools(ws).snapshot()["id"]
    assert ValidationEvidence(ws).inspect(snapshot, task_id=first)
    assert not ValidationEvidence(ws).inspect(snapshot, task_id=second)
    with pytest.raises(PilotError, match="another task"):
        ValidationEvidence(ws).inspect(snapshot, evidence["id"], task_id=second)
