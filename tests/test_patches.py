"""Exact patch validation, newline preservation, recovery, and rollback."""

from __future__ import annotations

import pytest

from repopilotv3.core import PilotError
from repopilotv3.manual.patches import PatchManager, parse_patch, transform
from repopilotv3.manual.sessions import SessionStore
from repopilotv3.repository.service import RepositoryTools
from repopilotv3.tasks.store import TaskStore
from test_workflow import proposal


def stage(ws, session, value=None):
    """Stage a proposal for one initialized task/chat binding."""
    task = TaskStore(ws).create("Fix greeting", seeds=["greeter.py"])
    SessionStore(ws).bind(session, task["id"])
    return PatchManager(ws).stage(value or proposal(ws), session, task["id"])["patch_id"]


def test_review_required_stale_rejected_and_reject_does_not_write(ws, session):
    """Require explicit review and reject proposals after intervening source edits."""
    manager = PatchManager(ws)
    key = stage(ws, session)
    with pytest.raises(PilotError):
        manager.apply(key, True)
    manager.review(key)
    (ws.root / "greeter.py").write_text("changed by editor")
    with pytest.raises(PilotError, match="stale"):
        manager.apply(key, True)
    assert (ws.root / "greeter.py").read_text() == "changed by editor"


@pytest.mark.parametrize(
    "path", ["../escape.py", "/escape.py", "C:/escape.py", ".repopilotv3/config.json", ".git/config"]
)
def test_escaping_paths_rejected(ws, session, path):
    """Never allow a proposed addition outside permitted repository source."""
    snap = RepositoryTools(ws).snapshot()
    value = {
        "base_snapshot": snap["id"],
        "base_hashes": {path: None},
        "diff": f"--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+hello\n",
    }
    with pytest.raises(PilotError):
        PatchManager(ws).validate(value)


def test_crlf_bom_additions_deletions_and_malformed_patches():
    """Keep original text conventions and reject unsupported/broken patch structure."""
    entry = parse_patch("--- a/hello.py\n+++ b/hello.py\n@@ -1 +1 @@\n-old\n+new\n")[0]
    assert transform(entry, b"\xef\xbb\xbfold\r\n") == b"\xef\xbb\xbfnew\r\n"
    add = parse_patch("--- /dev/null\n+++ b/new.py\n@@ -0,0 +1 @@\n+new\n")[0]
    assert transform(add, None) == b"new\n"
    delete = parse_patch("--- a/old.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-old\n")[0]
    assert transform(delete, b"old\n") is None
    for text in [
        "binary\0",
        "--- a/a.py\n+++ b/a.py\n@@ -1,2 +1 @@\n-old\n+new\n",
        "diff --git a/a b/a\nGIT binary patch",
    ]:
        with pytest.raises(PilotError):
            parse_patch(text)


@pytest.mark.parametrize("newline", ["lf", "crlf"])
def test_multi_file_write_failure_rolls_back(ws, session, monkeypatch, newline):
    """An injected second-file failure restores every already applied source file."""
    import repopilotv3.manual.patches as module

    original_other = b"old\r\n" if newline == "crlf" else b"old\n"
    proposed_other = b"new\r\n" if newline == "crlf" else b"new\n"
    (ws.root / "other.py").write_bytes(original_other)
    snap = RepositoryTools(ws).snapshot()
    value = {
        "base_snapshot": snap["id"],
        "base_hashes": {"greeter.py": snap["hashes"]["greeter.py"], "other.py": snap["hashes"]["other.py"]},
        "diff": proposal(ws)["diff"] + "--- a/other.py\n+++ b/other.py\n@@ -1 +1 @@\n-old\n+new\n",
    }
    manager = PatchManager(ws)
    key = stage(ws, session, value)
    manager.review(key)
    original = (ws.root / "greeter.py").read_bytes()
    real_write = module.atomic_bytes
    failed = False

    def fail_second(path, data):
        """Fail once during source application while allowing recovery writes."""
        nonlocal failed
        if path == ws.root / "other.py" and data == proposed_other and not failed:
            failed = True
            raise OSError("injected write failure")
        return real_write(path, data)

    monkeypatch.setattr(module, "atomic_bytes", fail_second)
    with pytest.raises(OSError):
        manager.apply(key, True)
    assert (ws.root / "greeter.py").read_bytes() == original
    assert failed
    assert (ws.root / "other.py").read_bytes() == original_other


def test_edit_between_preflight_and_backup_cannot_be_overwritten(ws, session, monkeypatch):
    """Retain an external edit made after hunk validation but before original-byte capture."""
    manager = PatchManager(ws)
    patch_id = stage(ws, session)
    manager.review(patch_id)
    validate_patch = manager.validate
    path = ws.root / "greeter.py"
    changed = path.read_bytes() + b"\n# Later developer edit\n"

    def changed_after_preflight(proposal):
        """Model an external editor racing the patch's backup boundary."""
        result = validate_patch(proposal)
        path.write_bytes(changed)
        return result

    monkeypatch.setattr(manager, "validate", changed_after_preflight)
    with pytest.raises(PilotError, match="recovery-copy capture"):
        manager.apply(patch_id, True)
    assert path.read_bytes() == changed
    assert ws.get("patch", patch_id)["status"] == "staged"
