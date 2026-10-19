"""Isolated synthetic repositories; tests never need model credentials or browser access."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from repopilotv3.core import Workspace, atomic_json
from repopilotv3.manual.sessions import SessionStore


@pytest.fixture
def ws(tmp_path):
    """Create a Unicode/space path with text-only analysis and synthetic Python sources."""
    spec = importlib.util.spec_from_file_location(
        "demo", Path(__file__).parents[1] / "examples/create_demo.py"
    )
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    root = demo.create(tmp_path / "Space Ω repository")
    workspace = Workspace(root)
    config = workspace.initialize()
    config["dotnet"] = None
    atomic_json(workspace.config_path, config)
    return workspace


@pytest.fixture
def session(ws):
    """Register an independent uninitialized local chat."""
    return SessionStore(ws).create(project="example-project")["id"]


def request(tool: str, arguments: dict, request_id: str = "one") -> str:
    """Serialize a versioned manual tool request for protocol tests."""
    return json.dumps(
        {"protocol_version": "1.0", "request_id": request_id, "tool": tool, "arguments": arguments}
    )
