"""Compact model-facing tool replies while retaining full private execution records."""

from __future__ import annotations

import copy

from repopilotv3.core import canonical


def is_tool_response(value) -> bool:
    """Recognize harness output without confusing a valid tool request with its result."""
    return isinstance(value, dict) and "tool" not in value and (
        value.get("kind") == "tool_results"
        or (value.get("protocol_version") == "1.0" and (
            isinstance(value.get("events"), list)
            or ("ok" in value and any(key in value for key in ("results", "data", "error")))
        ))
    )


class ReplyFormatter:
    """Remove transport noise and duplicated progress without clipping returned source or evidence."""

    @staticmethod
    def task(value: dict) -> dict:
        """Keep actionable progress and verification while removing internal timestamps and blanks."""
        result = copy.deepcopy(value)
        for key in ("created_at", "updated_at", "last_progress", "schema_version"):
            result.pop(key, None)
        for key in list(result):
            if result[key] in (None, [], {}) and key != "validation":
                del result[key]
        return result

    @staticmethod
    def error(value: dict) -> dict:
        """Replace legacy response-as-request payload echoes with a concise corrective explanation."""
        result = copy.deepcopy(value)
        message = result.get("message", "")
        if result.get("code") == "invalid_request" and isinstance(message, str) and (
            message.startswith("request: {'events':") or message.startswith('request: {"events":')
        ):
            result = {"code": "response_pasted", "message": "A RepoPilot tool response was pasted as a request. Paste it into the WebUI model; return its next tool requests here. No tools ran."}
        return result

    def compact(self, value: dict) -> dict:
        """Consolidate the latest task once and preserve every substantive tool result and omission."""
        result = copy.deepcopy(value)
        result["kind"] = "tool_results"
        result.setdefault("protocol_version", "1.0")
        result.pop("copy_text", None)
        result.pop("reply_id", None)
        warnings = list(result.pop("warnings", []))
        tasks = []
        for response in result.get("results", []):
            for container in (response, response.get("data", {})):
                if isinstance(container, dict):
                    monitoring = container.pop("monitoring", {})
                    warnings.extend(monitoring.get("warnings", []) if isinstance(monitoring, dict) else [])
                    if isinstance(monitoring, dict) and monitoring.get("coverage") not in {None, "managed"}:
                        warnings.append("Monitoring coverage: " + str(monitoring["coverage"]))
            response.pop("protocol_version", None)
            if response.get("session_id") == result.get("session_id"):
                response.pop("session_id", None)
            data = response.get("data")
            if isinstance(data, dict):
                data.pop("run_id", None)
                if isinstance(data.get("task"), dict):
                    data.pop("todo", None)
                    tasks.append(data["task"])
                if data.get("omissions") == []:
                    data.pop("omissions")
            if isinstance(response.get("error"), dict):
                response["error"] = self.error(response["error"])
        if tasks and len({task.get("id") for task in tasks}) == 1:
            latest = tasks[-1]
            result["task"] = self.task(latest)
            result["task_id"] = latest.get("id")
            for response in result.get("results", []):
                data = response.get("data", {})
                if isinstance(data.get("task"), dict):
                    task = data.pop("task")
                    if task.get("revision") != latest.get("revision"):
                        data["task_revision"] = task.get("revision")
        for response in result.get("results", []):
            if response.get("task_id") == result.get("task_id"):
                response.pop("task_id", None)
        if isinstance(result.get("error"), dict):
            result["error"] = self.error(result["error"])
        for failure in result.get("local_errors", []):
            if isinstance(failure.get("error"), dict):
                failure["error"] = self.error(failure["error"])
        for key in ("pending", "omissions", "remaining_request_ids"):
            if result.get(key) in (None, [], {}):
                result.pop(key, None)
        if warnings:
            result["warnings"] = list(dict.fromkeys(warnings))
        if result.get("local_errors"):
            result["next"] = self.next_step(result)
        else:
            result.setdefault("next", self.next_step(result))
        for response in result.get("results", []):
            if isinstance(response.get("data"), dict):
                response["data"].pop("next", None)
        return result

    @staticmethod
    def next_step(value: dict) -> str:
        """Offer an evidence-based next action rather than repeating an obsolete intermediate TODO."""
        if value.get("pending"):
            return "Local approval is required. Wait for its result before dependent work."
        if value.get("error") or value.get("local_errors"):
            return "Correct the reported error; no failed or interrupted action should be assumed successful."
        responses = value.get("results", [])
        for response in reversed(responses):
            if response.get("error"):
                return response.get("next") or "Correct the reported error before requesting dependent work."
            data = response.get("data", {})
            if data.get("indexed_file_count") == 0:
                return "No eligible files are indexed. For an intended new project, propose additions using this snapshot and null base hashes; otherwise verify the selected folder/exclusions."
            if data.get("search_kind") == "keyword" and not data.get("hits") and not data.get("symbols"):
                return "No keyword matches. Use short identifiers, search '*' to list paths, or request focused context."
        if value.get("task"):
            return "Task/TODO saved. Inspect focused repository context, then continue the next agreed item."
        if responses and isinstance(responses[-1].get("data"), dict) and responses[-1]["data"].get("next"):
            return responses[-1]["data"]["next"]
        return "Use these results to continue the task; request only missing context."

    def render(self, value: dict) -> tuple[str, dict]:
        """Return a short human summary plus compact, explicitly labeled JSON results."""
        payload = self.compact(value)
        count = len(payload.get("results", []))
        if payload.get("error") or payload.get("local_errors"):
            summary = "RepoPilot: action needs attention."
        elif count:
            summary = f"RepoPilot: {count} tool result{'s' if count != 1 else ''} · {payload.get('status', 'returned')}."
        else:
            summary = "RepoPilot: action needs attention." if payload.get("error") else "RepoPilot: local update."
        return summary + "\n\n```json\n" + canonical(payload) + "\n```", payload
