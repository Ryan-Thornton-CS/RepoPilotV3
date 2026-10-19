"""Optional real SDK/Roslyn/MSTest checks using explicitly provisioned synthetic fixtures."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from repopilotv3.core import Workspace, atomic_json, discover_dotnet
from repopilotv3.manual.validation import validate
from repopilotv3.repository.index import RepositoryIndex


@pytest.mark.parametrize("kind", ["modern", "framework", "inventor"])
def test_provisioned_sdk_analysis_build_and_tests(kind, tmp_path):
    """Verify semantic relationships and genuine tests; net48 execution needs Windows."""
    if kind != "modern" and os.name != "nt":
        pytest.skip("Native Windows is required for .NET Framework fixture execution")
    executable = os.environ.get("REPOPILOTV3_TEST_DOTNET") or discover_dotnet()
    root = Path(os.environ.get("REPOPILOTV3_FIXTURES", ".tools/fixtures")).resolve() / kind
    if not executable or not (root / "fixture.json").exists():
        pytest.skip("Explicitly provision SDK fixtures before SDK acceptance checks")
    ws = Workspace(root, tmp_path / "state")
    config = ws.initialize()
    config["dotnet"] = executable
    config["profiles"] = {
        "build": {"kind": "build", "args": ["Demo.csproj"], "timeout": 120},
        "tests": {
            "kind": "test",
            "args": ["Tests/Calculator.Tests.csproj"],
            "runner": "VSTest",
            "timeout": 120,
        },
    }
    atomic_json(ws.config_path, config)
    snapshot = RepositoryIndex(ws).refresh(force=True)
    assert snapshot["quality"] == "semantic", snapshot["diagnostics"]
    assert any(s["name"] == "Calculator" for s in snapshot["symbols"])
    assert {"calls", "implements", "implements_member"} <= {r["kind"] for r in snapshot["relationships"]}
    assert any(s["is_test"] for s in snapshot["symbols"])
    assert validate(ws, "build")["status"] == "passed"
    evidence = validate(ws, "tests")
    assert evidence["status"] == "passed", evidence
    assert any(int(c["executed"]) for c in evidence["test_counters"])
