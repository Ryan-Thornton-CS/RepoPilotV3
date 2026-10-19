"""Cross-platform bundle inputs and runtime-copy regressions without downloading dependencies."""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("bundle_build", ROOT / "tools/build.py")
BUILD = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(BUILD)


def builder(tmp_path):
    """Create a dependency-free builder instance for deterministic input transformations."""
    value = object.__new__(BUILD.BundleBuilder)
    value.cache = tmp_path
    return value


def test_target_requirements_use_target_markers_not_host(tmp_path):
    """Cross-building Windows on Linux retains Windows dependencies and their exact hashes."""
    requirements = tmp_path / "locked.txt"
    requirements.write_text(
        '# export header\ncommon==1 --hash=sha256:aaa\n'
        'windows==2 ; sys_platform == "win32" \\\n    --hash=sha256:bbb \\\n    --hash=sha256:ccc\n'
        'linux==3 ; sys_platform == "linux" --hash=sha256:ddd\n'
        'oldpython==4 ; python_version < "3.12" --hash=sha256:eee\n'
    )
    value = builder(tmp_path)
    windows = value.target_requirements("win32-x64", requirements).read_text()
    linux = value.target_requirements("linux-x64", requirements).read_text()
    assert "windows==2 --hash=sha256:bbb --hash=sha256:ccc" in windows
    assert "common==1" in windows and "linux==3" not in windows
    assert "linux==3" in linux and "windows==2" not in linux
    assert "oldpython" not in windows + linux and ";" not in windows + linux


def test_runtime_lock_is_explicit_and_complete():
    """Both supported hosts have immutable standalone versions, checksums and safe entry paths."""
    lock = json.loads((ROOT / "tools/runtime-lock.json").read_text())
    assert set(lock["platforms"]) == {"linux-x64", "win32-x64"}
    for platform, entry in lock["platforms"].items():
        assert entry["version"] == "3.12.14"
        assert re.fullmatch("[a-f0-9]{64}", entry["sha256"])
        assert entry["url"].startswith("https://releases.astral.sh/")
        assert "20260901" in entry["url"]
        assert ".." not in entry["python"].split("/")
        assert entry["python"].endswith(".exe") == platform.startswith("win32")


def test_runtime_copy_materializes_valid_links_and_ignores_unused_alias(tmp_path):
    """uv's dangling cross-platform convenience alias cannot break a valid Windows runtime."""
    source, destination = tmp_path / "source", tmp_path / "copy"
    source.mkdir()
    (source / "python.exe").write_bytes(b"MZruntime")
    try:
        (source / "python").symlink_to("bin/missing")
        (source / "alias.exe").symlink_to("python.exe")
    except OSError:
        pytest.skip("Host does not permit creation of test symlinks")
    builder(tmp_path).extract(source, destination)
    assert (destination / "python.exe").read_bytes() == b"MZruntime"
    assert (destination / "alias.exe").read_bytes() == b"MZruntime"
    assert not (destination / "alias.exe").is_symlink()
    assert not (destination / "python").exists()


def test_extension_uses_bundled_backend_without_installer():
    """No end-user installer layer or provisioning command remains in extension execution."""
    manifest = json.loads((ROOT / "extension/package.json").read_text())
    assert manifest["contributes"]["configuration"]["properties"]["repopilotv3.pythonPath"]["default"] == "bundled"
    assert not any(item["command"].endswith(".install") for item in manifest["contributes"]["commands"])
    for obsolete in ["install.py", "install.sh", "Install.cmd", "tools/package_installer.py",
                     "extension/src/install.ts", "extension/scripts/package.js"]:
        assert not (ROOT / obsolete).exists(), obsolete
    runtime = (ROOT / "extension/src/runtime.ts").read_text()
    assert "manifest.json" in runtime and "fetch(" not in runtime and "spawn(" not in runtime
