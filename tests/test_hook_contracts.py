"""Exercise documented Copilot command-hook payloads, exit codes and honest warning coverage."""

from __future__ import annotations

import io
import json
import subprocess
import sys

import pytest

from repopilotv3.core import load_json
from repopilotv3.integrations.hooks import HookAdapter, main
from repopilotv3.integrations.monitor import Monitor
from repopilotv3.repository.index import RepositoryIndex


@pytest.mark.parametrize("event", ["postToolUseFailure", "PostToolUseFailure"])
def test_failure_error_payload_warns_once_via_exit_two(ws, monkeypatch, capsys, event):
    """A command hook exposes recovery guidance via stdout/exit 2 only on the third failure."""
    monitor = Monitor(ws)
    monitor.bind("copilot-chat", "run-failure")

    def forbidden(*args, **kwargs):
        """Failure observations must not index source or wait for the repository writer."""
        raise AssertionError("Hook attempted repository analysis")

    monkeypatch.setattr(RepositoryIndex, "refresh", forbidden)
    for attempt in range(1, 5):
        payload = {"session_id": "copilot-chat", "timestamp": attempt, "tool_name": "bash",
                   "tool_input": {"command": "dotnet build --no-restore"}, "error": "SDK not found"}
        monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
        with ws.lock():
            code = main(["--repo", str(ws.root), "--event", event])
        output = capsys.readouterr().out.strip()
        if attempt == 3:
            assert code == 2 and "Repeated failures" in output
            assert "not observable" in output and not output.startswith("{")
        else:
            assert code == 0 and json.loads(output) == {}
    assert monitor.status()["calls"] == 4


def test_failure_changed_error_arguments_and_chat_do_not_share_repetition(ws):
    """Different diagnostics, commands and independent runs start separate advisory counters."""
    monitor = Monitor(ws)
    monitor.bind("A", "run-A")
    monitor.bind("B", "run-B")
    adapter = HookAdapter(ws)
    base = {"sessionId": "A", "toolName": "bash", "toolArgs": {"command": "build"}, "error": "missing SDK"}
    for attempt in [1, 2]:
        assert adapter.handle("postToolUseFailure", {**base, "timestamp": attempt}) == {}
    assert adapter.handle("postToolUseFailure", {**base, "timestamp": 3, "error": "compiler error"}) == {}
    assert adapter.handle("postToolUseFailure", {**base, "timestamp": 4, "toolArgs": {"command": "test"}}) == {}
    assert adapter.handle("postToolUseFailure", {**base, "timestamp": 5, "sessionId": "B"}) == {}
    assert "additionalContext" in adapter.handle("postToolUseFailure", {**base, "timestamp": 6})


def test_call_id_deduplicates_reobservations_despite_timestamp_changes(ws):
    """A repeated observation of one tool call cannot manufacture three failed executions."""
    monitor = Monitor(ws)
    monitor.bind("A", "run-A")
    adapter = HookAdapter(ws)
    base = {"sessionId": "A", "toolName": "bash", "toolArgs": {}, "error": "failed", "toolCallId": "call-1"}
    for stamp in range(4):
        assert adapter.handle("postToolUseFailure", {**base, "timestamp": stamp}) == {}
    assert monitor.status()["calls"] == 1
    assert adapter.handle("postToolUseFailure", {**base, "toolCallId": "call-2"}) == {}
    assert "additionalContext" in adapter.handle("postToolUseFailure", {**base, "toolCallId": "call-3"})


def test_opaque_success_does_not_claim_unchanged_source_and_hashed_reads_reset(ws):
    """Native output without source hashes stays partial; known source changes invalidate repeats."""
    monitor = Monitor(ws)
    monitor.bind("A", "run-A")
    adapter = HookAdapter(ws)
    base = {"sessionId": "A", "toolName": "view", "toolArgs": {"path": "source.py"}}
    for attempt in range(3):
        assert adapter.handle("postToolUse", {**base, "timestamp": attempt,
                             "toolResult": {"textResultForLlm": "same opaque source"}}) == {}
    for attempt in range(3, 6):
        result = adapter.handle("postToolUse", {**base, "timestamp": attempt,
                                "toolResult": {"hash": "original", "text": "source", "at": attempt}})
        assert bool(result) == (attempt == 5)
    assert adapter.handle("postToolUse", {**base, "timestamp": 6,
                          "toolResult": {"hash": "changed", "text": "new source"}}) == {}


def test_missing_error_and_checkpoint_binding_report_partial_coverage(ws):
    """Missing contract fields never become invented failure evidence or silent coverage claims."""
    adapter = HookAdapter(ws)
    base = {"sessionId": "A", "toolName": "bash", "timestamp": 1}
    assert adapter.handle("postToolUseFailure", base) == {}
    assert "missing error" in load_json(ws.state / "hook-health.json")["reason"]
    assert adapter.handle("postToolUseFailure", {**base, "error": "failed"}) == {}
    assert "no observed" in load_json(ws.state / "hook-health.json")["reason"]


def test_successful_operations_cannot_count_as_repeated_failures(ws):
    """Identical opaque bodies with different execution outcomes have independent counters."""
    monitor = Monitor(ws)
    for _ in range(2):
        assert not monitor.record("run", "tool", {}, {}, success=True)["warnings"]
    assert not monitor.record("run", "tool", {}, {}, success=False)["warnings"]
    assert monitor.record("run", "tool", {}, {}, success=True)["warnings"]


def test_hook_warning_exit_status_is_observable_in_real_subprocess(ws):
    """The executable entry point returns Copilot's warning status without executing a tool."""
    Monitor(ws).bind("actual-process", "run-process")
    command = [sys.executable, "-m", "repopilotv3.integrations.hooks", "--repo", str(ws.root), "--event", "postToolUseFailure"]
    for attempt in range(3):
        payload = {"sessionId": "actual-process", "toolName": "bash", "toolArgs": {},
                   "error": "synthetic failure", "timestamp": attempt}
        result = subprocess.run(command, input=json.dumps(payload), text=True, capture_output=True,
                                check=False, timeout=10)
        assert result.returncode == (2 if attempt == 2 else 0), result.stderr
        assert bool("Repeated failures" in result.stdout) == (attempt == 2)
    for event in ["preToolUse", "agentStop"]:
        result = subprocess.run([*command[:-1], event], input=json.dumps({"sessionId": "actual-process"}),
                                text=True, capture_output=True, check=False, timeout=10)
        assert result.returncode == 0 and json.loads(result.stdout) == {}
