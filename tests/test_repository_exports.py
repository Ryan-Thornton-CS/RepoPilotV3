"""Consistent context generation, source safety, and incremental reuse acceptance tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from repopilotv3.core import PilotError, Workspace, atomic_json, digest, git, map_root, run
from repopilotv3.exports.manager import ExportManager
from repopilotv3.manual.sessions import SessionStore, measure
from repopilotv3.repository.index import RepositoryIndex
from repopilotv3.repository.service import RepositoryTools, bounded
from repopilotv3.tasks.store import TaskStore


def bind(ws, session):
    """Create the export fixture's explicit selected task and initialized chat."""
    task = TaskStore(ws).create("Fix greeting punctuation", seeds=["greeter.py"])
    SessionStore(ws).bind(session, task["id"])
    return task


def test_selected_project_subfolder_excludes_siblings_and_private_state(tmp_path):
    """Choosing one project in a parent Git workspace keeps other projects out of context and Git."""
    parent = tmp_path / "Multi project Ω"
    parent.mkdir()
    assert run(["git", "init", "-q"], parent)["exit_code"] == 0
    first, second = parent / "Project A", parent / "Project B"
    first.mkdir()
    second.mkdir()
    (first / "main.py").write_text('"""Selected project."""\n', encoding="utf-8")
    (second / "unrelated.py").write_text('"""Other project."""\n', encoding="utf-8")
    workspace = Workspace(first)
    config = workspace.initialize()
    config["dotnet"] = None
    atomic_json(workspace.config_path, config)
    snapshot = RepositoryIndex(workspace).refresh()
    assert set(snapshot["hashes"]) == {"main.py"}
    assert "/Project A/.repopilotv3/" in (parent / ".git/info/exclude").read_text("utf-8")
    assert run(["git", "check-ignore", "-q", "Project A/.repopilotv3/config.json"], parent)["exit_code"] == 0


def test_three_layers_full_source_and_unchanged_reuse(ws, session):
    """Export maps/signatures/source, reuse unchanged artifacts, and never imply upload."""
    bind(ws, session)
    manager = ExportManager(ws)
    first = manager.prepare(session, True)
    directory = Path(first["directory"])
    assert all(
        (directory / name).is_file()
        for name in [
            "00_PROJECT_INSTRUCTIONS.md",
            "01_REPOSITORY_MAP.md",
            "02_FOCUSED_CONTEXT.md",
            "03_IMPLEMENTATION_CONTEXT.md",
            "05_TODO.md",
            "06_CONTINUATION.md",
        ]
    )
    overview = (directory / "01_REPOSITORY_MAP.md").read_text()
    assert "return f" not in overview
    focused = (directory / "02_FOCUSED_CONTEXT.md").read_text()
    assert "Greeter" in focused and "greeter.py" in focused
    assert 'return f"Hello {name}"' in (directory / "03_IMPLEMENTATION_CONTEXT.md").read_text()
    assert all(r["state"] == "never uploaded" for r in first["checklist"])
    confirmed = {
        "00_PROJECT_INSTRUCTIONS.md": first["manifest"]["artifacts"]["00_PROJECT_INSTRUCTIONS.md"]["hash"]
    }
    manager.confirm(first["manifest"]["id"], session, confirmed)
    second = manager.prepare(session, True)
    assert first["manifest"]["artifacts"] == second["manifest"]["artifacts"] or all(
        first["manifest"]["artifacts"][k]["hash"] == second["manifest"]["artifacts"][k]["hash"]
        for k in first["manifest"]["artifacts"]
    )
    (ws.root / "unrelated.txt").write_text("unrelated")
    third = manager.prepare(session, True)
    assert (
        third["manifest"]["artifacts"]["03_IMPLEMENTATION_CONTEXT.md"]["hash"]
        == second["manifest"]["artifacts"]["03_IMPLEMENTATION_CONTEXT.md"]["hash"]
    )
    assert third["checklist"][0]["action"] == "keep"
    (ws.root / "greeter.py").write_text((ws.root / "greeter.py").read_text().replace("Hello", "Welcome"))
    fourth = manager.prepare(session)
    assert (
        fourth["manifest"]["artifacts"]["03_IMPLEMENTATION_CONTEXT.md"]["hash"]
        != third["manifest"]["artifacts"]["03_IMPLEMENTATION_CONTEXT.md"]["hash"]
    )


def test_scope_separates_project_and_chat_confirmations(ws, session):
    """Project instructions may be reused, while another chat needs its own task packets."""
    task = bind(ws, session)
    manager = ExportManager(ws)
    first = manager.prepare(session)
    manager.confirm(
        first["manifest"]["id"],
        session,
        {name: artifact["hash"] for name, artifact in first["manifest"]["artifacts"].items()},
    )
    other = SessionStore(ws).create(task["id"], "example-project")["id"]
    second = manager.prepare(other)
    rows = {r["file"]: r for r in second["checklist"]}
    assert rows["00_PROJECT_INSTRUCTIONS.md"]["action"] == "keep"
    assert rows["06_CONTINUATION.md"]["action"] == "upload"
    assert other in (Path(second["directory"]) / "06_CONTINUATION.md").read_text()
    assert session not in (Path(second["directory"]) / "06_CONTINUATION.md").read_text()
    with pytest.raises(PilotError):
        manager.confirm(first["manifest"]["id"], other, {"05_TODO.md": "bad"})
    project = SessionStore(ws).create(task["id"], "different-project")["id"]
    assert manager.prepare(project)["checklist"][0]["action"] == "paste"


def test_renamed_deleted_oversized_secret_and_symlink_files(ws):
    """Refresh the same index safely after renames/deletions and omit unsafe sources."""
    (ws.root / "credentials.json").write_text('{"password":"private"}')
    (ws.root / ".env").write_text("TOKEN=private")
    (ws.root / "big.txt").write_text("x" * 1_000_001)
    try:
        (ws.root / "escape.py").symlink_to(Path(__file__))
    except OSError:
        pass
    index = RepositoryIndex(ws)
    snap = index.refresh()
    assert "credentials.json" not in snap["hashes"]
    assert (
        ".env" not in snap["hashes"] and "big.txt" not in snap["hashes"] and "escape.py" not in snap["hashes"]
    )
    (ws.root / "greeter.py").rename(ws.root / "renamed.py")
    (ws.root / "README.md").unlink()
    after = index.refresh()
    assert (
        "renamed.py" in after["hashes"]
        and "greeter.py" not in after["hashes"]
        and "README.md" not in after["hashes"]
    )


def test_unstable_capture_and_interrupted_publication(ws, session, monkeypatch):
    """Never publish mixed snapshots or replace the last good pointer on failure."""
    import repopilotv3.repository.index as module
    import repopilotv3.exports.manager as exporter

    bind(ws, session)
    manager = ExportManager(ws)
    good = manager.prepare(session)
    pointer = manager.base / f"current-{session}.json"
    before = pointer.read_bytes()
    original = module.capture
    counter = 0

    def changing(workspace):
        """Return a different captured body on every pass to model concurrent edits."""
        nonlocal counter
        counter += 1
        result = original(workspace)
        result["README.md"] = str(counter).encode()
        return result

    monkeypatch.setattr(module, "capture", changing)
    with pytest.raises(PilotError, match="changed during capture"):
        manager.prepare(session)
    assert pointer.read_bytes() == before
    monkeypatch.setattr(module, "capture", original)
    real_atomic = exporter.atomic_json

    def interrupted(path, value):
        """Simulate interruption immediately before publication of the generation pointer."""
        if path == pointer:
            raise OSError("interrupted publication")
        return real_atomic(path, value)

    monkeypatch.setattr(exporter, "atomic_json", interrupted)
    with pytest.raises(OSError):
        manager.prepare(session)
    assert pointer.read_bytes() == before
    assert Path(good["directory"]).is_dir()


def test_budget_preserves_units_and_reports_omissions(ws, session):
    """Large sources remain complete in full exports and explicit in bounded tool output."""
    bind(ws, session)
    text = '"""Large synthetic source."""\n' + "# repeated content\n" * 7000
    (ws.root / "large.py").write_text(text)
    result = RepositoryTools(ws).context("large", ["large.py"])
    assert any(x["path"] == "large.py" for x in result["omissions"])
    prepared = ExportManager(ws).prepare(session, True)
    full = next(name for name in prepared["manifest"]["artifacts"] if name.endswith("_large.py.md"))
    assert text in (Path(prepared["directory"]) / full).read_text()
    reduced = bounded({"sources": [{"path": "large.py", "hash": digest(text), "text": text}]})
    assert measure(json.dumps(reduced))["estimated_tokens"] < 4400
    assert reduced["omissions"]


def test_missing_sdk_degrades_and_windows_mapping(ws):
    """Missing semantic dependencies do not prevent useful text lookup or doctor output."""
    (ws.root / "Example.cs").write_text("public class Example { public int Value() => 1; }")
    config = ws.config
    config["dotnet"] = "/missing/dotnet"
    atomic_json(ws.config_path, config)
    snap = RepositoryIndex(ws).refresh()
    assert snap["quality"] == "text_only"
    assert any("unavailable" in d.lower() for d in snap["diagnostics"])
    assert map_root(ws.root, [{"local": str(ws.root.parent), "worker": "C:/Source Ω"}]).startswith(
        "C:/Source Ω/"
    )


def test_private_state_git_exclusion_and_config_ownership(ws):
    """Initialization excludes private state locally without modifying tracked ignore files."""
    from repopilotv3.core import Workspace, run

    run(["git", "init"], ws.root)
    workspace = Workspace(ws.root, ws.root / ".private-state")
    workspace.initialize()
    assert git(ws.root, "check-ignore", ".private-state/config.json").strip()
    config = workspace.config
    config["owner"]["system"] = "different"
    atomic_json(workspace.config_path, config)
    with pytest.raises(PilotError, match="another host"):
        workspace.config


def test_incremental_search_and_content_storage(ws):
    """Unchanged documents reuse content/FTS entries across newly captured snapshots."""
    index = RepositoryIndex(ws)
    before = index.refresh()
    with ws.db() as db:
        initial = db.execute("SELECT count(*) FROM search_documents").fetchone()[0]
    (ws.root / "README.md").write_text("Changed architecture description")
    after = index.refresh()
    with ws.db() as db:
        assert db.execute("SELECT count(*) FROM search_documents").fetchone()[0] == initial + 1
        assert db.execute("SELECT count(*) FROM contents").fetchone()[0] == initial + 1
    assert index.query("greet", snapshot=after)["hits"]
    assert before["id"] != after["id"]


def test_exported_json_valid_and_compact_prompt_budget(ws, session):
    """Generated schemas remain valid JSON and instruction headers fit the paste budget."""
    bind(ws, session)
    exported = ExportManager(ws).prepare(session)
    directory = Path(exported["directory"])
    assert json.loads((directory / "request.schema.json").read_text())["oneOf"]
    assert len((directory / "00_PROJECT_INSTRUCTIONS.md").read_text()) <= 8000


def test_all_private_generations_and_case_aliases_are_excluded(ws):
    """Private state from every RepoPilot generation remains inaccessible on all hosts."""
    from repopilotv3.core import safe_path

    for private in [".repopilot", ".repopilotv2", ".repopilotv3", ".REPOPILOTV2"]:
        folder = ws.root / "nested" / private
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "secret.py").write_text('"""Private fixture."""\n')
        with pytest.raises(PilotError):
            safe_path(ws.root, f"nested/{private}/secret.py")
    for alias in [".repopilotv3 /config.py", ".git./config.py", "NUL.py", "CON/source.py"]:
        with pytest.raises(PilotError):
            safe_path(ws.root, alias)
    snapshot = RepositoryTools(ws).snapshot()
    assert not any("repopilot" in path.casefold() for path in snapshot["hashes"])


def test_focused_context_cache_reuses_verified_snapshot_and_invalidates_changes(ws, monkeypatch):
    """Cached retrieval skips selection on unchanged input and refreshes when bytes change."""
    tools = RepositoryTools(ws)
    first = tools.context("greeting", ["greeter.py"])
    select = tools.index.select

    def unexpected(*args, **kwargs):
        """Fail if an unchanged context packet unnecessarily repeats semantic selection."""
        raise AssertionError("Expected cached context")

    monkeypatch.setattr(tools.index, "select", unexpected)
    assert tools.context("greeting", ["greeter.py"]) == first
    monkeypatch.setattr(tools.index, "select", select)
    path = ws.root / "greeter.py"
    path.write_text(path.read_text() + "\n# new source revision\n")
    changed = tools.context("greeting", ["greeter.py"])
    assert changed["snapshot"] != first["snapshot"]
    assert "new source revision" in next(s["text"] for s in changed["sources"] if s["path"] == "greeter.py")
