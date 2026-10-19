"""One versioned registry for manual request validation and published schemas."""

from __future__ import annotations

import json
from importlib.resources import files

from jsonschema import Draft202012Validator

from repopilotv3.core import PilotError

STRING = {"type": "string", "minLength": 1}
STRINGS = {"type": "array", "items": STRING, "maxItems": 100}
ITEM = {
    "type": "object",
    "required": ["id", "text", "status"],
    "properties": {
        "id": {**STRING, "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]{0,99}$"},
        "text": STRING,
        "status": {"enum": ["pending", "active", "blocked", "completed"]},
    },
    "additionalProperties": False,
}
PROGRESS = {
    "type": "object",
    "properties": {
        "items": {"type": "array", "items": ITEM, "maxItems": 100},
        **{
            k: STRINGS for k in ["errors", "files", "decisions", "questions", "assumptions", "required_tests"]
        },
    },
    "additionalProperties": False,
}


def obj(properties: dict, required: tuple = ()) -> dict:
    """Create a closed JSON object schema for tool arguments."""
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


ARGUMENTS = {
    "task": obj({"objective": {**STRING, "maxLength": 20000}, "task_id": STRING,
                 "chat_name": {**STRING, "maxLength": 160}, "seeds": STRINGS}),
    "todo": obj({"objective": STRING, "task_id": STRING, "progress": PROGRESS, "seeds": STRINGS}),
    "search": obj({"query": STRING, "limit": {"type": "integer", "minimum": 1, "maximum": 50}}, ("query",)),
    "read": obj(
        {
            "path": STRING,
            "start": {"type": "integer", "minimum": 1},
            "end": {"type": "integer", "minimum": 1},
        },
        ("path",),
    ),
    "context": obj({"query": STRING, "paths": STRINGS}),
    "changes": obj(
        {
            "validation_id": STRING,
            "start": {"type": "integer", "minimum": 1},
            "end": {"type": "integer", "minimum": 1},
        }
    ),
    "propose_patch": obj(
        {
            "base_snapshot": STRING,
            "base_hashes": {
                "type": "object",
                "minProperties": 1,
                "maxProperties": 100,
                "additionalProperties": {"type": ["string", "null"]},
            },
            "diff": STRING,
            "files": {
                "type": "object", "minProperties": 1, "maxProperties": 100,
                "description": "Complete proposed UTF-8 contents by repository-relative path; null deletes a file, empty string creates or empties it.",
                "additionalProperties": {"type": ["string", "null"]},
            },
        },
        ("base_snapshot", "base_hashes"),
    ) | {"oneOf": [{"required": ["diff"]}, {"required": ["files"]}]},
    "validate": obj({"profile": STRING}, ("profile",)),
    "handoff": obj({}),
    "run_command": obj({
        "argv": {"type": "array", "minItems": 1, "maxItems": 200,
                 "items": {**STRING, "maxLength": 20000}},
        "cwd": {**STRING, "maxLength": 2000},
        "timeout": {"type": "integer", "minimum": 1, "maximum": 3600},
    }, ("argv",)),
    "command_output": obj({"command_id": STRING,
                           "start": {"type": "integer", "minimum": 1},
                           "end": {"type": "integer", "minimum": 1}}, ("command_id",)),
}
READ_ONLY = {"search", "read", "context", "changes", "command_output"}
CALLS = [
    {
        "type": "object",
        "required": ["tool", "arguments"],
        "properties": {"tool": {"const": tool}, "arguments": args},
        "additionalProperties": False,
    }
    for tool, args in ARGUMENTS.items()
    if tool in READ_ONLY
]
ARGUMENTS["batch"] = obj(
    {"calls": {"type": "array", "items": {"oneOf": CALLS}, "minItems": 1, "maxItems": 8}}, ("calls",)
)
REQUEST_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": "https://repopilotv3.local/schemas/v1/request.schema.json",
    "oneOf": [
        {
            "type": "object",
            "required": ["protocol_version", "request_id", "tool", "arguments"],
            "properties": {
                "protocol_version": {"const": "1.0"},
                "request_id": {**STRING, "maxLength": 100},
                "tool": {"const": name},
                "arguments": args,
            },
            "additionalProperties": False,
        }
        for name, args in ARGUMENTS.items()
    ],
}


def validate(value: dict, schema: dict = REQUEST_SCHEMA) -> None:
    """Raise a readable structured error for an invalid tool message."""
    selected = schema
    if schema is REQUEST_SCHEMA and isinstance(value, dict):
        tool = value.get("tool")
        if isinstance(tool, str) and tool in ARGUMENTS:
            selected = next(s for s in schema["oneOf"] if s["properties"]["tool"]["const"] == tool)
        elif isinstance(tool, str):
            raise PilotError("Unknown tool '" + tool + "'. Available: " + ", ".join(ARGUMENTS), "unknown_tool")
    errors = list(Draft202012Validator(selected).iter_errors(value))
    if errors:
        error = errors[0]
        location = ".".join(str(p) for p in error.absolute_path) or "request"
        if error.validator == "oneOf" and error.schema == ARGUMENTS["propose_patch"]:
            raise PilotError(location + ": supply exactly one of files (complete proposed contents) or diff (unified patch), never both.", "invalid_request")
        raise PilotError(location + ": " + error.message[:700], "invalid_request")


def parse(text: str) -> dict:
    """Accept bounded JSON, optionally surrounded by one Markdown JSON fence."""
    if len(text.encode("utf-8")) > 2_500_000:
        raise PilotError("Request exceeds 2.5 MB", "invalid_request")
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines[0] not in {"```", "```json"} or lines[-1] != "```":
            raise PilotError("Use one complete JSON code fence", "invalid_request")
        text = "\n".join(lines[1:-1])
    try:
        result = json.loads(text)
    except (ValueError, RecursionError) as exc:
        raise PilotError(f"Invalid JSON: {exc}", "invalid_request") from exc
    validate(result)
    return result


def protocol_reference() -> str:
    """Load the packaged protocol reference without requiring the source checkout."""
    return files("repopilotv3.resources").joinpath("protocol.md").read_text("utf-8")
