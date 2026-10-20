"""Synthetic composer, editable prompt and shared-task reset regression coverage."""

from __future__ import annotations

import json
from importlib.resources import files

import pytest

from repopilotv3.core import PilotError, digest
from repopilotv3.manual.sessions import SessionStore
from repopilotv3.rpc.server import BridgeServer
from repopilotv3.tasks.store import TaskStore
from repopilotv3.workflow.roles import RolePrompts
from repopilotv3.workflow.service import WorkflowService


def request(tool, arguments, request_id):
    """Create a standalone versioned request for the automatic Send path."""
    return json.dumps(dict(protocol_version="1.0", request_id=request_id, tool=tool, arguments=arguments))


def test_send_routes_complete_reply_once_and_retains_plain_prose(ws, session):
    """Adjacent requests execute once; plain prose remains an outgoing developer message."""
    service = WorkflowService(ws)
    text = "First register the task.\n```json\n" + request("task", {"objective": "Inspect greeting"}, "task") + "\n" + request("todo", {"progress": {"items": [{"id": "inspect", "text": "Inspect greeting", "status": "active"}]}}, "todo") + "\n```"
    result = service.submit(session, text)
    assert result["ok"] and len(result["results"]) == 2
    task = service.status(session)["task"]
    assert service.submit(session, text)["reply_id"] == result["reply_id"]
    assert service.status(session)["task"]["revision"] == task["revision"]
    assert service.submission_preview("Please explain this method")["route"] == "message"
    assert service.submission_preview("[Help](https://example.com)")["route"] == "message"
    outgoing = service.submit(session, "Please explain this method")
    assert "Please explain" in outgoing["copy_text"] and "Mode:" not in outgoing["copy_text"]
    assert "Mode:" not in service.starter(session)["text"]


@pytest.mark.parametrize("text", ['{"tool":', 'Commands:\n```json\n{"tool":"read",}\n```', '{"protocol_version":"1.0","kind":"tool_results","results":[]}', '```json\n{"kind":"tool_results","results":[]}\n```'])
def test_send_does_not_forward_malformed_requests_or_tool_responses(ws, session, text):
    """Request-shaped failures are durable and execute nothing rather than becoming prose."""
    service = WorkflowService(ws)
    result = service.submit(session, text)
    assert not result["ok"] and result["error"]["code"] in {"invalid_request", "response_pasted"}
    assert not [entry for entry in service.history.page(session) if entry["body"].get("action") == "forward"]
    assert not ws.records("task")


def test_prompt_files_are_authoritative_exact_and_recoverable(ws):
    """Saved CRLF/Unicode guidance survives new services; reset backs up its exact bytes."""
    store = RolePrompts(ws)
    original = store.read("planning")
    path = store.path("planning")
    authored = "# Custom Ω\r\nAnswer briefly.\r\n"
    path.write_bytes(authored.encode("utf-8"))
    prompt = RolePrompts(ws).read("planning")
    assert prompt["text"] == authored and prompt["customized"]
    with pytest.raises(PilotError, match="changed"):
        store.reset("planning", original["hash"])
    reset = store.reset("planning", prompt["hash"])
    from pathlib import Path
    assert (Path(reset["recovery"]) / "planning.md").read_bytes() == authored.encode("utf-8")
    assert store.read("planning")["text"] == store.default("planning")
    with pytest.raises(PilotError):
        store.read("../secret")
    assert digest(path.read_bytes()) == reset["hash"]
    assert files("repopilotv3.resources").joinpath("prompts/review.md").read_text("utf-8")


def test_reset_checklist_archives_shared_progress_and_preserves_evidence(ws, session):
    """Local reset retains facts and blocks both continuation chats until TODO initialization."""
    service = WorkflowService(ws)
    service.submit(session, request("todo", {"objective": "Inspect greeting", "progress": {
        "items": [{"id": "inspect", "text": "Inspect", "status": "active"}], "required_tests": ["pytest"]}}, "init"))
    task = service.status(session)["task"]
    task["validation"] = [{"id": "actual-record", "status": "passed"}]
    TaskStore(ws).save(task)
    continuation = service.sessions.create(task["id"], "other-project")["id"]
    service.sessions.bind(continuation, task["id"])
    with pytest.raises(PilotError, match="changed"):
        service.reset_checklist(session, task["revision"] - 1)
    reset = service.reset_checklist(session, task["revision"])
    updated = TaskStore(ws).get(task["id"])
    assert not updated["items"] and updated["required_tests"] == ["pytest"]
    assert updated["validation"] == task["validation"] and updated["history"][-1]["items"] == task["items"]
    assert set(reset["affected_sessions"]) == {session, continuation}
    assert all(not SessionStore(ws).get(chat)["todo_ready"] for chat in (session, continuation))
    rejected = service.submit(continuation, request("read", {"path": "greeter.py"}, "read-after-reset"))
    assert not rejected["ok"] and rejected["results"][0]["error"]["code"] == "todo_required"
    assert "Reset checklist" not in json.dumps(__import__('repopilotv3.manual.schema', fromlist=['ARGUMENTS']).ARGUMENTS)


def test_management_ownership_and_prompt_actions_do_not_create_chats(ws, session):
    """Prompt APIs are administrative; composer requires a valid project-owned conversation."""
    server = BridgeServer(ws)
    try:
        prompt = server.call("prompts.read", {"role": "asking"})
        assert prompt["text"].startswith("# Asking")
        assert len(server.call("sessions.list", {})) == 1
        with pytest.raises(PilotError):
            server.call("composer.submit", {"session_id": "unknown", "text": "hello"})
    finally:
        server.executor.shutdown()
        server.admin.shutdown()


def test_distribution_contains_full_vsix_and_clean_backend_sources(tmp_path):
    """ZIP includes explicit public inputs, hashes and the complete backend without private state."""
    import importlib.util
    import zipfile
    from pathlib import Path

    root = Path(__file__).parents[1]
    spec = importlib.util.spec_from_file_location("release", root / "tools/release.py")
    release = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(release)
    archive = release.ReleaseArchive(root)
    payload = archive.inputs()
    assert "backend/src/repopilotv3/integrations/mcp.py" in payload
    assert "backend/src/repopilotv3/workflow/roles.py" in payload
    assert ".github/agents/repopilotv3-code.agent.md" in payload
    assert ".mcp.json" in payload and "alternatives/.vscode/mcp.json" in payload
    assert all(".repopilotv3/" not in key and "__pycache__" not in key for key in payload)
    # Exercise the actual release build against synthetic public inputs and a versioned dummy bundle.
    synthetic = tmp_path / "release"
    synthetic.mkdir()
    (synthetic / "pyproject.toml").write_text('[project]\nversion="0.2.12"\n', "utf-8")
    (synthetic / "dist").mkdir()
    with zipfile.ZipFile(synthetic / "dist/repopilotv3-0.2.12.vsix", "w") as vsix:
        vsix.writestr("extension/package.json", '{"version":"0.2.12"}')
        vsix.writestr("extension/backend/manifest.json", '{"backend_version":"0.2.12"}')
    builder = release.ReleaseArchive(synthetic)
    builder.inputs = lambda: payload
    target = builder.build()
    with zipfile.ZipFile(target) as result:
        checksum_text = result.read(builder.prefix + "SHA256SUMS").decode("utf-8")
        assert "repopilotv3-0.2.12.vsix" in checksum_text
        assert result.read(builder.prefix + "backend/src/repopilotv3/workflow/roles.py") == payload["backend/src/repopilotv3/workflow/roles.py"]
    assert target.exists() and (target.parent / "SHA256SUMS").exists()


def test_legacy_mode_is_readable_but_cannot_influence_new_prompts(ws, session):
    """Old mode metadata survives loading while new chats and handoffs have no mode field."""
    raw = ws.get("session", session)
    raw["mode"] = "review"
    ws.put("session", session, raw)
    service = WorkflowService(ws)
    assert service.sessions.get(session)["mode"] == "review"
    assert "Mode:" not in service.starter(session)["text"]
    service.configure(session, mode="code")
    assert service.sessions.get(session)["mode"] == "review"
    assert "mode" not in service.create()["session"]
