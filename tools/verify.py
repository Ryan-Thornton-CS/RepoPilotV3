"""Verify and optionally exercise the complete VSIX outside the development environment."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
import tomllib
import threading
import zipfile
from pathlib import Path


def verify(vsix: Path, root: Path) -> dict:
    """Check both native runtimes, locked backend resources and byte-identical static files."""
    lock = json.loads((root / "tools/runtime-lock.json").read_text(encoding="utf-8"))
    with zipfile.ZipFile(vsix) as archive:
        names = set(archive.namelist())
        assert all(not Path(name).is_absolute() and ".." not in name.split("/") for name in names)
        package = json.loads(archive.read("extension/package.json"))
        assert package["engines"]["vscode"] == "^1.85.0"
        assert not package.get("enabledApiProposals")
        assert package["contributes"]["configuration"]["properties"]["repopilotv3.pythonPath"]["default"] == "bundled"
        assert not any("node_modules/" in name or "/__pycache__/" in name for name in names)
        assert "extension/out/src/install.js" not in names
        assert "extension/backend/install.py" not in names
        for directory in ["harness", "docs"]:
            for path in (root / directory).rglob("*"):
                if path.is_file():
                    member = "extension/" + path.relative_to(root).as_posix()
                    assert archive.read(member) == path.read_bytes(), member
        for name in ["out/src/extension.js", "out/src/backend.js", "out/src/runtime.js",
                     "media/panel.js", "media/panel.css", "out/src/mcp.js", "out/src/configuration.js", "out/vendor/jsonc-parser/main.js", "out/vendor/jsonc-parser/LICENSE.md", "LICENSE.txt"]:
            assert archive.read("extension/" + name)
        manifest = json.loads(archive.read("extension/backend/manifest.json"))
        assert manifest["schema_version"] == "1.0" and manifest["backend_version"] == package["version"] == tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
        assert set(manifest["platforms"]) == set(lock["platforms"])
        for platform, item in manifest["platforms"].items():
            assert item["archive_sha256"] == lock["platforms"][platform]["sha256"]
            interpreter = archive.read("extension/backend/" + item["python"])
            assert interpreter.startswith(b"MZ" if platform.startswith("win32") else b"\x7fELF")
            prefix = "extension/backend/" + platform + "/python/" + lock["platforms"][platform]["site_packages"] + "/"
            for resource in ["repopilotv3/cli.py", "repopilotv3/resources/webui.md",
                             "repopilotv3/resources/schemas/v1/request.schema.json",
                             "fastmcp/__init__.py", "pytest/__init__.py", "pythonnet/__init__.py"]:
                assert archive.read(prefix + resource), prefix + resource
            for source in (root / "src/repopilotv3").rglob("*"):
                if source.is_file() and "__pycache__" not in source.parts:
                    member = prefix + source.relative_to(root / "src").as_posix()
                    assert archive.read(member) == source.read_bytes(), member
            assert any(name.startswith(prefix) and ".dist-info/licenses/" in name for name in names)
    return {"vsix": str(vsix), "bytes": vsix.stat().st_size,
            "platforms": list(manifest["platforms"]), "static_harness": "byte-identical",
            "minimum_editor": "1.85.0"}


def smoke_bundle(vsix: Path) -> dict:
    """Extract a host runtime with Unicode/spaces and prove imports need no outbound connections."""
    platform = "win32-x64" if os.name == "nt" else "linux-x64"
    with tempfile.TemporaryDirectory(prefix="RepoPilotV3 VSIX Ω ") as temporary:
        root = Path(temporary)
        with zipfile.ZipFile(vsix) as archive:
            manifest = json.loads(archive.read("extension/backend/manifest.json"))
            for entry in archive.infolist():
                if entry.filename.startswith("extension/backend/" + platform + "/") or entry.filename.startswith("extension/harness/") or entry.filename == "extension/backend/manifest.json":
                    archive.extract(entry, root)
                    permissions = (entry.external_attr >> 16) & 0o777
                    if permissions and os.name != "nt":
                        (root / entry.filename).chmod(permissions)
        python = root / "extension/backend" / manifest["platforms"][platform]["python"]
        if os.name != "nt":
            python.chmod(0o755)
        code = """
import socket

def denied(*args, **kwargs):
    raise AssertionError('An offline import attempted a network connection')

socket.socket.connect = denied
socket.create_connection = denied
socket.getaddrinfo = denied
import fastmcp, pytest, pythonnet, jsonschema, watchdog, sqlite3
from repopilotv3.integrations.mcp import create_server
connection = sqlite3.connect(':memory:')
connection.execute('CREATE VIRTUAL TABLE source USING fts5(body)')
print('offline dependency imports and FTS5 passed')
"""
        subprocess.run([str(python), "-I", "-B", "-c", code], cwd=root, check=True, timeout=60)
        return {"host": platform, "isolated_offline_imports": "passed", **smoke(str(python))}


def smoke(python: str) -> dict:
    """Run setup, manual TODO/read/patch, genuine check evidence, and a new-chat handoff."""
    with tempfile.TemporaryDirectory(prefix="RepoPilotV3 bundle Ω ") as temporary:
        base = Path(temporary)
        root = base / "source"
        root.mkdir()
        original = '"""Synthetic greeting fixture."""\n\ndef greet():\n    """Return the greeting."""\n    return "Hello"\n'
        source = root / "greeting.py"
        source.write_text(original)
        (root / "test_greeting.py").write_text(
            '"""Synthetic actual test evidence."""\nfrom greeting import greet\n'
            'def test_greeting():\n    """Check the edited greeting."""\n    assert greet() == "Hello!"\n'
        )
        command = [python, "-I", "-B", "-u", "-m", "repopilotv3", "--repo", str(root)]

        def cli(*arguments: str) -> dict:
            """Run the bundled distribution without importing source-tree modules."""
            result = subprocess.run(
                [*command, *arguments],
                cwd=root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=60,
                check=False,
            )
            assert result.returncode == 0, {"arguments": arguments, "stdout": result.stdout, "stderr": result.stderr}
            return json.loads(result.stdout)

        cli("setup")
        config_path = root / ".repopilotv3/config.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["dotnet"] = None
        config["profiles"] = {
            "check": {
                "kind": "check",
                "args": [],
                "timeout": 10,
                "argv": [
                    python,
                    "-c",
                    "from greeting import greet; assert greet() == 'Hello!'; print('checked actual source')",
                ],
            }
        }
        config["profiles"]["tests"] = {
            "kind": "python_test", "timeout": 30, "args": [],
            "argv": [python, "-B", "-m", "pytest", "-q", "--junitxml={results_dir}/junit.xml"],
        }
        config["default_validation"] = "check"
        config_path.write_text(json.dumps(config), encoding="utf-8")

        def rpc(method: str, parameters: dict) -> dict:
            """Keep the stdio connection open until the result, as a real extension does."""
            frame = {"jsonrpc": "2.0", "id": 1, "method": method, "params": {
                "bridge_version": "1.0", "workspace": str(root), **parameters}}
            process = subprocess.Popen([*command, "bridge", "serve"], cwd=root, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
            watchdog = threading.Timer(60, process.kill)
            watchdog.daemon = True
            watchdog.start()
            try:
                process.stdin.write(json.dumps(frame) + "\n")
                process.stdin.flush()
                while line := process.stdout.readline():
                    message = json.loads(line)
                    if message.get("id") == 1:
                        assert "result" in message, message
                        return message["result"]
                raise AssertionError(method + " backend disconnected or exceeded 60 seconds: " + process.stderr.read())
            finally:
                watchdog.cancel()
                process.stdin.close()
                process.wait(timeout=60)
                process.stdout.close()
                process.stderr.close()

        if os.name == "nt":
            assert Path(rpc("hello", {"workspace": str(root).lower()})["workspace"]) == root.resolve()
        chat = rpc("sessions.create", {})["session"]["id"]
        starter = rpc("prompts.starter", {"session_id": chat})
        assert chat in starter["copy_text"] and "command_output" not in starter["copy_text"]
        project_prompt = rpc("prompts.project", {})
        assert "command_output" in project_prompt["copy_text"] and len(project_prompt["copy_text"]) <= project_prompt["max_characters"] == 8000
        messages = [
            {"protocol_version": "1.0", "request_id": "new-task", "tool": "task",
             "arguments": {"objective": "Check chat commands", "chat_name": "Bundled chat"}},
            {"protocol_version": "1.0", "request_id": "new-todo", "tool": "todo", "arguments": {}},
        ]
        imported = rpc("replies.import", {"session_id": chat, "text": "Planning this.\n```json\n" + "\n".join(
            json.dumps(message) for message in messages) + "\n```"})
        assert imported["status"] == "completed", imported
        copied = rpc("history.copy_turn", {"session_id": chat, "entry_id": "reply-result:" + imported["reply_id"]})
        copied_results = copied["copy_data"]["results"]
        assert [result["request_id"] for result in copied_results] == ["new-task", "new-todo"]
        role_prompt = rpc("prompts.read", {"role": "implementation"})
        assert "Implementation" in role_prompt["text"]
        assert rpc("composer.preview", {"session_id": chat, "text": "Discuss next steps"})["route"] == "message"
        tool = {"protocol_version": "1.0", "request_id": "new-command", "tool": "run_command",
                "arguments": {"argv": ["python", "-c", "from pathlib import Path; Path('command_observed.py').write_text('command_value = 42\\n'); print('actual bundled command')"]}}
        pending = rpc("replies.import", {"session_id": chat, "text": json.dumps(tool)})
        assert pending["status"] == "awaiting_permission" and not pending["copy_text"], pending
        finished = rpc("replies.resolve", {"session_id": chat, "reply_id": pending["reply_id"], "approved": True})
        executed = finished["results"][0]["data"]
        assert executed["status"] == "completed" and executed["exit_code"] == 0, executed
        assert "actual bundled command" in executed["output"] and "Fatal Python" not in executed["output"]
        observed = rpc("commands.diffs", {"session_id": chat, "command_id": executed["command_id"]})
        assert any(file["path"] == "command_observed.py" for file in observed["files"]), observed
        session = cli("session", "create", "--objective", "Fix greeting", "--name", "Bundle smoke")["session"][
            "id"
        ]

        def request(tool: str, args: dict, request_id: str) -> dict:
            """Keep input messages outside source so writing them cannot stale a patch base."""
            path = base / "request.json"
            path.write_text(
                json.dumps(
                    {"protocol_version": "1.0", "request_id": request_id, "tool": tool, "arguments": args}
                )
            )
            response = cli("request", str(path), "--session", session)
            assert response["ok"], response
            return response

        todo = request("todo", {}, "todo")
        task = todo["task_id"]
        current = request("read", {"path": "greeting.py"}, "read")["data"]
        modified = original.replace('return "Hello"', 'return "Hello!"')
        patch = request(
            "propose_patch",
            {
                "base_snapshot": current["snapshot"],
                "base_hashes": {"greeting.py": current["hash"]},
                "files": {"greeting.py": modified},
            },
            "patch",
        )["data"]["patch_id"]
        cli("patch", "review", patch, "--session", session)
        applied = cli("patch", "apply", patch, "--session", session, "--confirm-reviewed")
        assert source.read_text(encoding="utf-8") == modified
        assert applied["data"]["validation"]["status"] == "passed", applied
        tested = request("validate", {"profile": "tests"}, "test-evidence")["data"]["validation"]
        assert tested["status"] == "passed" and tested["evidence_kind"] == "tests", tested
        assert sum(int(c["executed"]) for c in tested["test_counters"]) == 1, tested
        request("todo", {"progress": {"decisions": ["Verified by actual check and pytest"]}}, "checkpoint")
        frame = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "handoff",
            "params": {
                "bridge_version": "1.0",
                "workspace": str(root),
                "session_id": session,
                "confirmed_new_chat": True,
            },
        }
        result = subprocess.run(
            [*command, "bridge", "serve"],
            input=json.dumps(frame) + "\n",
            cwd=root,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=60,
            check=True,
        )
        continuation = next(
            json.loads(line)["result"]
            for line in result.stdout.splitlines()
            if json.loads(line).get("id") == 1
        )
        assert continuation["session_id"] != session
        assert continuation["manifest"]["task_id"] == task
        bootstrap = (Path(continuation["directory"]) / "06_CONTINUATION.md").read_text(encoding="utf-8")
        assert continuation["session_id"] in bootstrap
        rpc("sessions.update", {"session_id": session, "archived": True})
        assert any(row["id"] == session for row in rpc("sessions.list", {"archived": True}))
        rpc("sessions.update", {"session_id": session, "archived": False})
        assert rpc("sessions.delete", {"session_id": session})["deleted"]
        assert all(row["id"] != session for row in rpc("sessions.list", {}))
        assert rpc("sessions.status", {"session_id": continuation["session_id"]})["task"]["id"] == task
        assert not (root / ".github").exists() and not (root / ".mcp.json").exists()
        harness_preview = rpc("harness.preview", {"server_name": "repopilotv3-bundle"})
        assert not (root / ".github").exists() and not harness_preview["conflicts"]
        harness_result = rpc("harness.apply", {"plan_id": harness_preview["plan_id"]})
        assert harness_result["status"] == "generated" and (root / ".github/agents/repopilotv3-code.agent.md").exists()
        assert not (root / ".mcp.json").exists()
        bootstrap = base / "MCP fresh project"
        bootstrap.mkdir()
        (bootstrap / "sample.py").write_text('"""Standalone MCP fixture."""\n', encoding="utf-8")
        probe = """
import asyncio, sys
from fastmcp import Client

async def main():
    config = {"mcpServers": {"probe": {"command": sys.executable, "args": ["-I", "-B", "-m", "repopilotv3", "--repo", sys.argv[1], "mcp", "serve"]}}}
    async with Client(config) as client:
        assert len(await client.list_tools()) == 6
        assert not (await client.call_tool("repository_status")).data["initialized"]
        binding = (await client.call_tool("setup_repository")).data
        assert binding["initialized"] and not binding["harness_written"]
        result = await client.call_tool("read_source", {"path": "sample.py", "repository_id": binding["repository_id"]})
        assert "Standalone MCP fixture" in result.data["data"]["text"]
asyncio.run(main())
"""
        subprocess.run([python, "-I", "-B", "-c", probe, str(bootstrap)], cwd=base, check=True, timeout=60)
        return {
            "bundled_backend_workflow": "passed",
            "standalone_mcp": "real stdio discovery; private setup; bounded retrieval",
            "chat_workflow": "starter; task/TODO/naming; editable role prompts; permission; actual command and observed source diff",
            "validation": "actual local check and one JUnit-evidenced pytest",
            "handoff": "independent chat; retained task",
            "harness_written": "only after explicit management preview/apply",
        }


def main() -> None:
    """Inspect the single release artifact and optionally run a synthetic host workflow."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("vsix", nargs="?", type=Path)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text())["project"]["version"]
    vsix = args.vsix or root / f"dist/repopilotv3-{version}.vsix"
    result = verify(vsix, root)
    if args.smoke:
        result["smoke"] = smoke_bundle(vsix)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
