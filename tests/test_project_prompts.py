"""Separate reusable project policy from fresh-chat introductions and continuation state."""

from __future__ import annotations

import io
import json
import re

import pytest
from jsonschema import Draft202012Validator

from conftest import request
from repopilotv3.core import PilotError
from repopilotv3.exports.instructions import ProjectInstructions
from repopilotv3.manual.schema import ARGUMENTS, REQUEST_SCHEMA
from repopilotv3.manual.sessions import measure
from repopilotv3.repository.index import RepositoryIndex
from repopilotv3.rpc.server import BridgeServer
from repopilotv3.workflow.service import WorkflowService


def test_project_prompt_is_standalone_budgeted_and_schema_correct(ws):
    """Project guidance is complete, bounded and accessible before any task or chat exists."""
    server = BridgeServer(ws, io.StringIO())
    try:
        prompt = server.call("prompts.project", {})
        text = prompt["text"]
        assert 1000 < prompt["characters"] == len(text) <= prompt["max_characters"] == 8000
        assert len(text.replace("\n", "\r\n")) <= 8000
        assert ws.root.name not in text and not ws.records("session")
        assert not ws.records("task") and "Repository orientation" not in text
        for tool in ARGUMENTS:
            assert f"**{tool}:**" in text
        examples = re.findall(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
        assert len(examples) >= 4
        for example in examples:
            Draft202012Validator(REQUEST_SCHEMA).validate(json.loads(example))
    finally:
        server.executor.shutdown()
        server.admin.shutdown()


def test_new_starter_does_not_index_repeat_policy_or_invent_an_objective(ws, session, monkeypatch):
    """Fresh-chat introductions work while repository work is busy and contain no project payload."""
    service = WorkflowService(ws)

    def forbidden(*args, **kwargs):
        """Prevent starter generation from doing source analysis or context publication."""
        raise AssertionError("A fresh starter must not index the repository")

    monkeypatch.setattr(RepositoryIndex, "refresh", forbidden)
    with ws.lock():
        starter = service.starter(session)
    text = starter["text"]
    assert session in text and ws.root.name in text and "Planning" in text
    assert "no task or objective" in text and "ask" in text.lower()
    assert "```" not in text and "Registered tools" not in text and "Public declarations" not in text
    assert ProjectInstructions().render() not in text
    assert measure(text)["estimated_tokens"] < 350
    assert not service.status(session)["task"] and service.starter(session) == starter


def test_legacy_combined_starter_is_replaced_only_on_explicit_request(ws, session):
    """New starter copies migrate cached combined prompts without rewriting historical messages."""
    service = WorkflowService(ws)
    legacy = {"text": "Old combined instructions and code", "copy_text": "Old combined instructions and code"}
    service.history.preference("starter:" + session, legacy)
    service.history.add(session, "harness", legacy, "old-starter")
    service.status(session)
    assert service.history.preference("starter:" + session) == legacy
    fresh = service.starter(session)
    assert fresh["starter_version"] == 2 and fresh["text"] != legacy["text"]
    assert service.history.page(session)[0]["body"] == legacy
    assert service.history.preference("starter:" + session) == fresh


def test_instruction_changes_replace_only_their_export_even_without_source_edits(ws, session, monkeypatch):
    """Prompt revisions invalidate publication cache and scoped receipts independently of source."""
    service = WorkflowService(ws)
    first = service.prepare(session)
    name = "00_PROJECT_INSTRUCTIONS.md"
    from pathlib import Path

    assert (Path(first["directory"]) / name).read_text("utf-8") == ProjectInstructions().render()
    service.confirm(session, first["manifest"]["id"], {name: first["manifest"]["artifacts"][name]["hash"]})
    original = ProjectInstructions().render()
    monkeypatch.setattr(ProjectInstructions, "render", lambda self: original + "\nAdditional project guidance.\n")
    second = service.prepare(session)
    assert second["manifest"]["snapshot"] == first["manifest"]["snapshot"]
    assert second["manifest"]["id"] != first["manifest"]["id"]
    assert second["manifest"]["artifacts"][name]["hash"] != first["manifest"]["artifacts"][name]["hash"]
    assert second["manifest"]["artifacts"]["01_REPOSITORY_MAP.md"]["hash"] == first["manifest"]["artifacts"]["01_REPOSITORY_MAP.md"]["hash"]
    assert next(row for row in second["checklist"] if row["file"] == name)["action"] == "replace"


def test_continuation_retains_progress_and_own_identity_without_repeating_project_policy(ws, session):
    """A continuation carries task facts, not a second copy of reusable behavior or schemas."""
    service = WorkflowService(ws)
    service.execute(session, request("todo", {"objective": "Continue this objective", "progress": {"decisions": ["Preserve the public API"]}}))
    next_chat = service.handoff(session, True)
    text = next_chat["copy_text"]
    assert next_chat["session_id"] in text and next_chat["session_id"] != session
    assert "Continue this objective" in text and "Preserve the public API" in text
    assert ProjectInstructions().render() not in text and "### Registered tools" not in text
    assert not service.sessions.get(next_chat["session_id"])["todo_ready"]
    assert not service.sessions.get(next_chat["session_id"])["commands_allowed"]


def test_project_prompt_budget_fails_explicitly_instead_of_truncating_rules(monkeypatch):
    """A future oversized instruction revision must never silently discard operating rules."""
    monkeypatch.setattr(ProjectInstructions, "MAX_CHARACTERS", 100)
    with pytest.raises(PilotError) as error:
        ProjectInstructions().render()
    assert error.value.code == "prompt_budget"
    assert "character" in str(error.value)


def test_character_budget_accepts_exact_limit_and_rejects_short_token_estimates():
    """Enforce actual pasted characters even when the token estimate is much smaller."""
    instructions = ProjectInstructions()
    instructions.check_budget("x" * 8000)
    with pytest.raises(PilotError) as error:
        instructions.check_budget("x" * 8001)
    assert error.value.code == "prompt_budget"
