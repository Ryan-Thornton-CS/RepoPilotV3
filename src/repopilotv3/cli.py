"""Argparse entry point for private setup, diagnostics, stdio servers, and recovery."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from repopilotv3.core import PilotError, Workspace, atomic_json


def configure_profile(ws: Workspace, choice: str) -> None:
    """Save a developer-selected validation profile; never infer approval from model text."""
    config = ws.config
    if choice == "keep":
        return
    if choice == "python":
        name = "tests"
        profile = {
            "kind": "python_test",
            "timeout": 300,
            "args": [],
            "argv": [sys.executable, "-m", "pytest", "--junitxml={results_dir}/junit.xml"],
        }
    elif choice in {"dotnet_test", "dotnet_build"}:
        name = "tests" if choice.endswith("test") else "build"
        profile = {"kind": "test" if choice.endswith("test") else "build", "timeout": 300, "args": []}
        if profile["kind"] == "test":
            profile["runner"] = "VSTest"
    else:
        raise PilotError("Unknown validation selection")
    if name in config["profiles"] and config["profiles"][name] != profile:
        raise PilotError(
            f"Profile {name} already exists; configure a different name locally", "profile_conflict"
        )
    config["profiles"][name] = profile
    config["default_validation"] = name
    atomic_json(ws.config_path, config)


def parser() -> argparse.ArgumentParser:
    """Define private setup, stdio servers, and explicit operational recovery commands."""
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--repo", default=".", help="Explicit repository root")
    result.add_argument("--state", help="Optional private state location owned by this host")
    commands = result.add_subparsers(dest="command", required=True)
    setup = commands.add_parser("setup", help="Initialize private state; never write workspace harness files")
    setup.add_argument(
        "--validation", choices=["keep", "python", "dotnet_test", "dotnet_build"], default="keep"
    )
    commands.add_parser("doctor", help="Report selected SDK and degraded capabilities")
    for name in ["mcp", "bridge"]:
        command = commands.add_parser(name, help="Serve local stdio " + name)
        command.add_argument("action", choices=["serve"])
    request = commands.add_parser("request", help="Execute a manual JSON file")
    request.add_argument("file", type=Path)
    request.add_argument("--session", required=True)
    request.add_argument("--reply", action="store_true", help="Import prose and explicit JSON request blocks")
    session = commands.add_parser("session", help="Create, list, or inspect local chats")
    session.add_argument("action", choices=["create", "list", "status", "configure"])
    session.add_argument("--session")
    session.add_argument("--task")
    session.add_argument("--objective")
    session.add_argument("--name", default="New session")
    session.add_argument("--mode", choices=["plan", "ask", "code", "review"])
    session.add_argument("--context-limit", type=int)
    session.add_argument("--project")
    permission = session.add_mutually_exclusive_group()
    permission.add_argument("--allow-commands", action="store_true")
    permission.add_argument("--revoke-commands", action="store_true")
    patch = commands.add_parser("patch", help="Inspect, approve, reject, or recover a complete proposal")
    patch.add_argument("action", choices=["review", "apply", "reject", "recover"])
    patch.add_argument("patch_id")
    patch.add_argument("--session", required=True)
    patch.add_argument("--confirm-reviewed", action="store_true")
    export = commands.add_parser("export", help="Prepare an immutable upload package")
    export.add_argument("--session", required=True)
    export.add_argument("--full-source", action="store_true")
    return result


def main(argv=None) -> int:
    """Dispatch local actions without installing dependencies or modifying a static harness."""
    # Stdio protocols and redirected CLI output are UTF-8 on Windows as well.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    ws = Workspace(args.repo, args.state)
    try:
        if args.command == "setup":
            ws.initialize()
            with ws.lock():
                configure_profile(ws, args.validation)
            result = {
                "config": str(ws.config_path),
                "harness_written": False,
                "next": "Manually merge harness/workspace if Copilot integration is wanted.",
            }
        elif args.command == "bridge":
            from repopilotv3.rpc.server import BridgeServer

            BridgeServer(ws).serve()
            return 0
        elif args.command == "mcp":
            from repopilotv3.integrations.mcp import serve

            serve(ws)
            return 0
        else:
            from repopilotv3.workflow.service import WorkflowService

            ws.config
            service = WorkflowService(ws)
            if args.command == "doctor":
                result = service.diagnostics()
            elif args.command == "request":
                operation = service.import_reply if args.reply else service.execute
                result = operation(args.session, args.file.read_text("utf-8"))
            elif args.command == "session":
                if args.action == "create":
                    result = service.create(args.name, args.task, args.objective, args.project)
                elif args.action == "list":
                    result = service.sessions.list()
                elif args.action == "configure":
                    if not args.session:
                        raise PilotError("Pass --session for configuration")
                    settings = {key: value for key, value in {"mode": args.mode, "context_limit": args.context_limit,
                                "project": args.project}.items() if value is not None}
                    if args.allow_commands or args.revoke_commands:
                        settings["commands_allowed"] = args.allow_commands
                    result = service.configure(args.session, **settings)
                else:
                    if not args.session:
                        raise PilotError("Pass --session for status")
                    result = service.status(args.session)
            elif args.command == "patch":
                if args.action == "review":
                    result = service.review(args.session, args.patch_id)
                elif args.action in {"apply", "reject"}:
                    if args.action == "apply" and not args.confirm_reviewed:
                        raise PilotError(
                            "Review the diff and pass --confirm-reviewed", "confirmation_required"
                        )
                    result = service.approve(args.session, args.patch_id, args.action == "apply")
                else:
                    result = service.recover(args.session, args.patch_id)
            elif args.command == "export":
                result = service.prepare(args.session, args.full_source)
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (PilotError, OSError, ValueError) as exc:
        print(
            json.dumps({"error": {"code": getattr(exc, "code", "local_error"), "message": str(exc)}}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
