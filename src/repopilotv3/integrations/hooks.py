"""Bounded warning-only Copilot hooks; no indexing, builds, transcript reads, or forced turns."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from repopilotv3.core import PilotError, Workspace, atomic_json, canonical, digest, now
from repopilotv3.integrations.monitor import Monitor, stable

ALIASES = {
    "SessionStart": "sessionStart",
    "UserPromptSubmit": "userPromptSubmitted",
    "PreToolUse": "preToolUse",
    "PostToolUse": "postToolUse",
    "PostToolUseFailure": "postToolUseFailure",
    "PreCompact": "preCompact",
    "Stop": "agentStop",
}


def object_value(value) -> dict:
    """Read bounded object-shaped native or MCP payloads without evaluating code."""
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and len(value) <= 256000:
        try:
            result = json.loads(value)
            return result if isinstance(result, dict) else {}
        except ValueError:
            pass
    return {}


def unwrap(value) -> dict:
    """Find structured MCP results nested in native result envelopes."""
    body = object_value(value)
    for key, value in body.items():
        if key.lower() in {"structuredcontent", "textresultforllm", "text_result_for_llm"}:
            nested = object_value(value)
            if nested:
                return nested
    for block in body.get("content", [])[:8]:
        parsed = object_value(block.get("text")) if isinstance(block, dict) else {}
        if parsed:
            return parsed
    return body


class HookAdapter:
    """Observe available native events without pretending to cover the whole chat."""

    def __init__(self, ws):
        """Reuse only the independent monitoring database."""
        self.ws = ws
        self.monitor = Monitor(ws)

    def handle(self, event: str, payload: dict) -> dict:
        """Normalize hook payloads, deduplicate server events, and always allow stopping."""
        event = ALIASES.get(event, event)
        session = payload.get("sessionId", payload.get("session_id"))
        if not isinstance(session, str) or not session or len(session) > 256:
            return {}
        if event not in {"postToolUse", "postToolUseFailure"}:
            if event == "agentStop":
                atomic_json(self.ws.paths.runtime("last-stop.json"), {"at": now(), "session": digest(session)})
            return {}
        tool = payload.get("toolName", payload.get("tool_name", "unknown"))
        if not isinstance(tool, str) or len(tool) > 256:
            return {}
        raw_args = payload.get("toolArgs", payload.get("tool_input", {}))
        args = object_value(raw_args)
        if not args and not isinstance(raw_args, dict):
            args = {"opaque_arguments": digest(canonical(raw_args))}
        result = unwrap(payload.get("toolResult", payload.get("tool_result", {})))
        failure = event == "postToolUseFailure"
        if failure:
            error = payload.get("error")
            if not isinstance(error, str) or not error.strip():
                atomic_json(self.ws.paths.runtime("hook-health.json"), {
                    "coverage": "partial", "reason": "failure event missing error text", "at": now(),
                })
                return {}
            result = {"error": error}
        monitor = object_value(result.get("monitoring"))
        if monitor.get("event_id"):
            with self.monitor.db() as db:
                observed = db.execute(
                    "SELECT run,tool FROM events WHERE id=?", (monitor["event_id"],)
                ).fetchone()
            if (
                observed
                and observed[0] == monitor.get("run_id")
                and "repopilot" in tool
                and observed[1] in tool
            ):
                if "task_checkpoint" in tool:
                    self.monitor.bind(session, observed[0])
                return {}
        run_id = self.monitor.bound(session)
        if not run_id:
            atomic_json(self.ws.paths.runtime("hook-health.json"), {
                "coverage": "partial", "reason": "no observed task checkpoint binding", "at": now(),
            })
            return {}
        correlation = payload.get("toolCallId", payload.get("tool_call_id"))
        if not isinstance(correlation, str) or not correlation or len(correlation) > 256:
            correlation = None
        stamp = payload.get("timestamp")
        if correlation is None and stamp is None:
            atomic_json(
                self.ws.paths.runtime("hook-health.json"),
                {"coverage": "partial", "reason": "ambiguous correlation", "at": now()},
            )
            return {}
        identity = [session, event, correlation] if correlation is not None else [session, event, stamp, tool, args]
        event_id = "hook-" + digest(canonical(identity))
        # Failures can repeat without proving unchanged source. Opaque successful reads cannot.
        fingerprint_args = {
            **args,
            "observed_result": digest(canonical(stable(result))),
            "source_observation": result.get("hash", "unobserved" if failure else event_id),
        }
        metadata = self.monitor.record(
            run_id,
            tool,
            fingerprint_args,
            result or {"opaque": True},
            success=not failure,
            event_id=event_id,
            coverage="partial",
        )
        return (
            {"additionalContext": "RepoPilotV3: " + " ".join(metadata["warnings"])}
            if metadata["warnings"]
            else {}
        )


def main(argv=None) -> int:
    """Consume bounded stdin and fail open with compatible empty output on monitoring errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--state")
    parser.add_argument("--event", required=True)
    args = parser.parse_args(argv)
    ws = Workspace(args.repo, args.state)
    try:
        raw = sys.stdin.read(256001)
        if len(raw) > 256000:
            raise ValueError("oversized payload")
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise ValueError("object required")
        ws.config
        output = HookAdapter(ws).handle(args.event, payload)
    except (PilotError, OSError, ValueError, TypeError, KeyError, sqlite3.Error, RecursionError) as exc:
        output = {}
        if ws.config_path.exists() and getattr(exc, "code", "") not in {"wrong_owner", "wrong_repository"}:
            try:
                atomic_json(
                    ws.paths.runtime("hook-health.json"),
                    {"coverage": "degraded", "at": now(), "event": args.event, "error": type(exc).__name__},
                )
            except OSError:
                pass
    if ALIASES.get(args.event, args.event) == "postToolUseFailure" and output.get("additionalContext"):
        # Copilot command hooks append stdout to the failed tool only on exit 2.
        # Never use this exit path for PreToolUse: exit 2 would deny the tool.
        print(output["additionalContext"])
        return 2
    print(json.dumps(output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
