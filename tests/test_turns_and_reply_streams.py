"""Verify complete turn copying and safe multi-request WebUI reply transports."""

from __future__ import annotations

import json

import pytest

from conftest import request
from repopilotv3.core import PilotError, canonical
from repopilotv3.workflow.replies import parse_reply
from repopilotv3.workflow.service import WorkflowService
from repopilotv3.workflow.turns import ConversationTurns


@pytest.mark.parametrize("transport", ["fenced", "plain", "array", "pretty", "crlf"])
def test_multiple_requests_in_one_payload_are_all_validated(transport):
    """Accept newline/adjacent objects, pretty-printed streams and arrays without losing prose."""
    calls = [request("task", {"objective": "Add hello_world.py and a test", "chat_name": "Hello world"}, "task-001"),
             request("todo", {"progress": {"items": [{"id": "inspect", "text": "Inspect conventions", "status": "active"}]}}, "todo-001")]
    if transport == "array":
        body = json.dumps([json.loads(call) for call in calls])
    elif transport == "pretty":
        body = "\n".join(json.dumps(json.loads(call), indent=2) for call in calls)
    else:
        body = "\n".join(calls)
    text = body if transport == "plain" else "I will register the task and checklist.\n```json\n" + body + "\n```"
    if transport == "crlf":
        text = (text + "\n").replace("\n", "\r\n")
    prose, decoded = parse_reply(text)
    assert [call["request_id"] for call in decoded] == ["task-001", "todo-001"]
    assert bool(prose) == (transport != "plain")


def test_invalid_later_object_duplicate_ids_and_request_limits_prevent_all_effects(ws, session):
    """A valid task followed by an invalid request cannot partially initialize a chat."""
    service = WorkflowService(ws)
    first = request("task", {"objective": "Must not be created"}, "first")
    invalid = {"protocol_version": "1.0", "request_id": "second", "tool": "todo", "arguments": {"unknown": True}}
    for tail in [json.dumps(invalid), '{"tool":"todo", broken}']:
        result = service.import_reply(session, "```json\n" + first + "\n" + tail + "\n```")
        assert not result["ok"] and result["error"]["code"] == "invalid_request"
        assert service.status(session)["task"] is None
    for body, code in [(first + "\n" + first, "request_conflict"),
                       ("\n".join(request("todo", {}, str(i)) for i in range(9)), "invalid_request")]:
        with pytest.raises(PilotError) as error:
            parse_reply("```json\n" + body + "\n```")
        assert error.value.code == code


def test_stream_executes_in_order_once_and_counts_one_imported_reply(ws, session):
    """The developer's two-object fence creates task/TODO once and keeps one recorded message."""
    service = WorkflowService(ws)
    text = "Registering task and TODO.\n```json\n" + request("task", {"objective": "Hello world", "chat_name": "Hello"}, "task") + "\n" + request("todo", {}, "todo") + "\n```"
    result = service.import_reply(session, text)
    assert result["status"] == "completed" and len(result["results"]) == 2
    assert service.import_reply(session, text) == result
    assert service.status(session)["session"]["todo_ready"]
    assert len([entry for entry in service.history.page(session) if entry["kind"] == "model"]) == 1


def test_copy_turn_includes_all_results_once_across_pagination_and_preserves_metadata(ws, session):
    """One anchored copy includes paged-out results, failures and remaining IDs without duplicates."""
    service = WorkflowService(ws)
    history = service.history
    history.add(session, "model", {"text": "Run several calls"}, "model-start")
    first = {"request_id": "a", "ok": True, "data": {"text": "A" * 15000, "hash": "hash-a", "snapshot": "source"}}
    second = {"request_id": "b", "ok": False, "error": {"code": "not_found", "message": "Missing source"}}
    history.add(session, "request", {"text": "original request", "request_id": "a"}, "request-a")
    history.add(session, "result", first, "result-a")
    history.add(session, "result", second, "result-b")
    aggregate = {"results": [first, second], "status": "failed", "remaining_request_ids": ["c"]}
    history.add(session, "harness", {"text": "Copy the complete reply", "copy_text": canonical(aggregate)}, "final")
    history.add(session, "user", {"text": "Next task"}, "next-user")
    assert history.page(session, before=history.page(session)[-1]["sequence"], limit=1)[0]["id"] == "final"
    turn = ConversationTurns(history).copy(session, "final")
    data = turn["copy_data"]
    results = data["results"]
    assert results == [first, second]
    assert data["remaining_request_ids"] == ["c"]
    assert "model-start" not in turn["entry_ids"] and "next-user" not in turn["entry_ids"]
    assert first["data"]["text"] in turn["copy_text"]


def test_user_copy_uses_forward_prompt_and_model_copy_retains_raw_tools(ws, session):
    """A user's prepared prompt has one copy target; the original incoming reply stays intact."""
    service = WorkflowService(ws)
    forwarded = service.compose(session, "Please inspect this")
    turn = ConversationTurns(service.history).copy(session, forwarded["entry_id"])
    assert turn["role"] == "user" and turn["copy_text"] == forwarded["copy_text"]
    assert len(turn["entry_ids"]) == 2
    raw = "Explanation\n```json\n" + request("todo", {"objective": "Inspect this"}, "todo") + "\n```"
    service.import_reply(session, raw)
    model = next(entry for entry in service.history.page(session) if entry["kind"] == "model")
    assert ConversationTurns(service.history).copy(session, model["id"])["copy_text"] == raw
    other = service.create()["session"]["id"]
    with pytest.raises(PilotError):
        ConversationTurns(service.history).copy(other, model["id"])


def test_combined_reply_does_not_truncate_earlier_tool_results(ws, session):
    """The aggregate keeps every already-bounded operation instead of imposing a second cap."""
    from repopilotv3.workflow.prompts import PromptService

    results = [{"request_id": str(i), "data": {"text": str(i) * 8000}} for i in range(3)]
    result = json.loads(PromptService(WorkflowService(ws)).copy_reply(session, {"results": results}))
    assert result["results"] == results


def test_panel_task_reply_does_not_wait_for_repository_analysis_or_package_generation(ws, session, monkeypatch):
    """Management replies mark packages dirty; explicit/background export performs the source work."""
    import io

    from repopilotv3.repository.index import RepositoryIndex
    from repopilotv3.rpc.server import BridgeServer

    server = BridgeServer(ws, io.StringIO())

    def forbidden(*args, **kwargs):
        """Fail if a task-only foreground reply attempts source capture or analysis."""
        raise AssertionError("Foreground TODO reply attempted repository analysis")

    try:
        with monkeypatch.context() as patch:
            patch.setattr(RepositoryIndex, "refresh", forbidden)
            text = "```json\n" + request("task", {"objective": "Task without an index"}, "task") + "\n" + request("todo", {}, "todo") + "\n```"
            result = server.call("replies.import", {"session_id": session, "text": text})
            assert result["status"] == "completed"
            assert server.call("sessions.status", {"session_id": session})["package_stale"]
            assert server.service.history.preference("package-dirty:" + session) is True
        prepared = server.call("exports.prepare", {"session_id": session})
        assert prepared["manifest"]
        assert server.service.history.preference("package-dirty:" + session) is False
    finally:
        server.executor.shutdown()
        server.admin.shutdown()
