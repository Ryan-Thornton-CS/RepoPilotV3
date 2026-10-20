"""Copilot installation, focused MCP, independent monitoring, and hook compatibility."""

from __future__ import annotations

import ast
import json
from importlib.resources import files
from pathlib import Path

import pytest
import yaml
from fastmcp import Client

from repopilotv3.core import PilotError
from repopilotv3.integrations.hooks import HookAdapter, main as hook_main
from repopilotv3.integrations.mcp import create_server
from repopilotv3.integrations.monitor import Monitor
from repopilotv3.manual.schema import REQUEST_SCHEMA, parse, validate
from repopilotv3.manual.sessions import measure
from repopilotv3.repository.service import RepositoryTools


def test_static_harness_agents_permissions_and_budgets():
    """Keep substantive static agents and the four-tool native Copilot boundary."""
    harness = Path(__file__).parents[1] / "harness/workspace"
    shared = (harness / ".github/copilot-instructions.md").read_text(encoding="utf-8")
    assert measure(shared)["estimated_tokens"] < 600
    for role in ["plan", "code", "ask", "review"]:
        text = (harness / f".github/agents/repopilotv3-{role}.agent.md").read_text(encoding="utf-8")
        front = yaml.safe_load(text.split("---")[1])
        assert "repopilotv3/*" in front["tools"]
        assert "model" not in front and "agent" not in front["tools"]
        assert len(front["handoffs"]) == 3 and all(not h["send"] for h in front["handoffs"])
        assert 3500 <= measure(text)["estimated_tokens"] <= 5500
        for word in ["Role", "principles", "workflow", "evidence", "Handoff", "Response"]:
            assert word.casefold() in text.casefold()
        if role == "code":
            assert {"edit", "execute"} <= set(front["tools"])
        elif role in {"plan", "ask"}:
            assert "edit" not in front["tools"] and "execute" not in front["tools"]
    assert len(files("repopilotv3.resources").joinpath("webui.md").read_text(encoding="utf-8")) <= 8000


def test_setup_never_writes_workspace_harness(ws):
    """Private setup preserves unrelated workspace configuration and instructions byte for byte."""
    from repopilotv3.cli import main

    (ws.root / ".github").mkdir()
    instructions = ws.root / ".github/copilot-instructions.md"
    instructions.write_text("Keep my instructions.\n")
    (ws.root / ".mcp.json").write_text('{"mcpServers":{"other":{"command":"other"}}}')
    before = {str(p): p.read_bytes() for p in [instructions, ws.root / ".mcp.json"]}
    assert main(["--repo", str(ws.root), "setup"]) == 0
    assert before == {p: Path(p).read_bytes() for p in before}
    assert not (ws.root / ".github/agents").exists()
    source = Path(__file__).parents[1] / "src/repopilotv3"
    assert not (source / "integrations/installer.py").exists()
    assert not (source / "tui").exists()


def test_static_host_commands_use_stdin():
    """Static hook commands contain no payload interpolation or generated host paths."""
    harness = Path(__file__).parents[1] / "harness/workspace"
    hooks = json.loads((harness / ".github/hooks/repopilotv3.json").read_text(encoding="utf-8"))["hooks"]
    for entries in hooks.values():
        hook = entries[0]
        assert hook["bash"].startswith("python -m repopilotv3.integrations.hooks")
        assert "$" not in hook["bash"] and "$" not in hook["powershell"]
        assert hook["timeoutSec"] <= 5
    server = json.loads((harness / ".mcp.json").read_text(encoding="utf-8"))["mcpServers"]["repopilotv3"]
    assert isinstance(server["args"], list)


async def test_mcp_has_setup_status_and_four_focused_tools(ws):
    """Use the real MCP client to verify retrieval and persistent fallback state."""
    async with Client(create_server(ws)) as client:
        names = {t.name for t in await client.list_tools()}
        assert names == {"setup_repository", "repository_status", "search_repository", "read_source", "get_context", "task_checkpoint"}
        source = await client.call_tool("read_source", {"path": "greeter.py"})
        assert source.data["data"]["hash"] == RepositoryTools(ws).read("greeter.py")["hash"]
        task = await client.call_tool("task_checkpoint", {"objective": "Fix greeting"})
        task_id = task.data["data"]["task"]["id"]
        run_id = task.data["data"]["run_id"]
        assert ws.get("run", run_id)["task_id"] == task_id
        resumed = await client.call_tool(
            "task_checkpoint",
            {"task_id": task_id, "run_id": run_id, "progress": {"decisions": ["Keep public signature"]}},
        )
        assert "Keep public signature" in resumed.data["data"]["task"]["decisions"]


def test_monitor_independent_runs_repeats_source_changes_and_checkpoint_replay(ws):
    """Warn once on repeats while preserving counters across identical checkpoints."""
    tools = RepositoryTools(ws)
    first = tools.checkpoint(objective="One task")
    second = tools.checkpoint(task_id=first["task"]["id"])
    assert first["run_id"] != second["run_id"]
    monitor = Monitor(ws)
    for run in [first["run_id"], second["run_id"]]:
        assert not monitor.record(run, "read", {"path": "x"}, {"text": "a"}, "snapshot")["warnings"]
        assert not monitor.record(run, "read", {"path": "x"}, {"text": "a"}, "snapshot")["warnings"]
        assert monitor.record(run, "read", {"path": "x"}, {"text": "a"}, "snapshot")["warnings"]
        assert not monitor.record(run, "read", {"path": "x"}, {"text": "a"}, "snapshot")["warnings"]
        assert not monitor.record(run, "read", {"path": "x"}, {"text": "b"}, "changed")["warnings"]
    before = monitor.status()["calls"]
    monitor.checkpoint(first["task"], first["run_id"])
    monitor.checkpoint(first["task"], first["run_id"])
    assert monitor.status()["calls"] == before


def test_hooks_bind_observed_checkpoint_deduplicate_and_allow_stop(ws):
    """Observe an actual saved MCP event; opaque and stop events cannot force continuation."""
    service = RepositoryTools(ws)
    saved = service.checkpoint(objective="Hook test")
    response = service.managed("task_checkpoint", {}, saved, saved["run_id"])
    adapter = HookAdapter(ws)
    body = {
        "sessionId": "external-chat",
        "toolName": "mcp__repopilotv3__task_checkpoint",
        "toolResult": {"structuredContent": response},
        "timestamp": 1,
    }
    before = Monitor(ws).status()["calls"]
    assert adapter.handle("postToolUse", body) == {}
    assert Monitor(ws).bound("external-chat") == saved["run_id"]
    assert Monitor(ws).status()["calls"] == before
    assert adapter.handle("Stop", {"session_id": "external-chat"}) == {}
    assert adapter.handle("PreToolUse", {"session_id": "external-chat"}) == {}
    assert adapter.handle("PostToolUse", {"session_id": "external-chat", "tool_name": "unknown"}) == {}
    assert Monitor(ws).status()["coverage"].startswith("partial")


@pytest.mark.parametrize(
    "payload",
    ["not json", "[]", "x" * 256001, '{"sessionId":false}'],
    ids=["malformed", "array", "oversized", "invalid-session"],
)
def test_hook_malformed_input_fails_open(ws, monkeypatch, capsys, payload):
    """Expected monitoring failures produce compatible empty output and exit success."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    assert hook_main(["--repo", str(ws.root), "--event", "agentStop"]) == 0
    assert json.loads(capsys.readouterr().out) == {}


def test_protocol_examples_schema_and_fenced_json():
    """Every published protocol example validates against the installed request schema."""
    import re

    body = files("repopilotv3.resources").joinpath("protocol.md").read_text(encoding="utf-8")
    for example in re.findall(r"```json\n(.*?)\n```", body, re.S):
        validate(json.loads(example))
        assert parse("```json\n" + example + "\n```") == json.loads(example)
    assert (
        json.loads(files("repopilotv3.resources").joinpath("schemas/v1/request.schema.json").read_text(encoding="utf-8"))
        == REQUEST_SCHEMA
    )
    with pytest.raises(PilotError):
        parse('{"protocol_version":"1.0","request_id":"bad","tool":"shell","arguments":{"command":"whoami"}}')


def test_all_python_modules_classes_and_functions_have_docstrings():
    """Keep implementation, examples, tooling, and tests documented through AST inspection."""
    root = Path(__file__).parents[1]
    missing = []
    for folder in ["src", "tests", "examples", "tools"]:
        for path in (root / folder).rglob("*.py"):
            tree = ast.parse(path.read_text("utf-8"))
            for node in ast.walk(tree):
                if isinstance(
                    node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
                ) and not ast.get_docstring(node):
                    missing.append(f"{path.relative_to(root)}:{getattr(node, 'lineno', 1)}")
    assert not missing, missing


def test_validation_retries_ignore_timing_but_reset_on_profile_or_source(ws):
    """Repeated failed evidence warns once; changed profile/source starts fresh comparisons."""
    run = RepositoryTools(ws).checkpoint(objective="Validation retries")["run_id"]
    monitor = Monitor(ws)
    for count in range(3):
        result = {
            "validation": {
                "status": "failed",
                "profile": "tests",
                "profile_hash": "p1",
                "test_counters": [{"failed": "1"}],
                "at": str(count),
            }
        }
        record = monitor.record(run, "validate", {"profile": "tests"}, result, "source1", success=False)
    assert any("failure" in w.lower() for w in record["warnings"])
    result["validation"]["profile_hash"] = "p2"
    assert not monitor.record(run, "validate", {"profile": "tests"}, result, "source1", success=False)[
        "warnings"
    ]
    assert not monitor.record(run, "validate", {"profile": "tests"}, result, "source2", success=False)[
        "warnings"
    ]


def test_overdue_checkpoint_warns_once_and_replay_cannot_reset(ws):
    """Twenty ordinary operations recommend a checkpoint; identical progress does not reset."""
    service = RepositoryTools(ws)
    saved = service.checkpoint(objective="Bound work")
    monitor = Monitor(ws)
    warnings = []
    for index in range(21):
        warnings.extend(monitor.record(saved["run_id"], "read", {"path": str(index)}, {}, "same")["warnings"])
    assert sum("checkpoint" in w.lower() for w in warnings) == 1
    before = monitor.status()["calls"]
    monitor.checkpoint(saved["task"], saved["run_id"])
    assert monitor.status()["calls"] == before


def test_unknown_hook_state_and_monitor_failure_are_compatible(ws, tmp_path, monkeypatch, capsys):
    """Uninitialized state and unavailable monitoring fail open without taking an index lock."""
    import io

    monkeypatch.setattr("sys.stdin", io.StringIO('{"sessionId":"unknown"}'))
    assert hook_main(["--repo", str(tmp_path), "--event", "postToolUse"]) == 0
    assert json.loads(capsys.readouterr().out) == {}
    assert not (tmp_path / ".repopilotv3").exists()
