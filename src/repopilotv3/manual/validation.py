"""Only explicit local process runs may create validation facts."""

from __future__ import annotations

import json
import os
import sys
import xml.etree.ElementTree as ET

from repopilotv3.core import (
    PilotError,
    Workspace,
    atomic_json,
    canonical,
    digest,
    dotnet_env,
    now,
    run,
    uid,
)
from repopilotv3.repository.index import RepositoryIndex
from repopilotv3.tasks.store import TaskStore


def validate(ws: Workspace, profile_name: str, task_id: str | None = None) -> dict:
    """Execute a selected build/test profile and record snapshot-bound local evidence."""
    config = ws.config
    profile = config["profiles"].get(profile_name)
    if not profile or profile.get("kind") not in {"build", "test", "python_test", "check"}:
        raise PilotError("Select a configured build or test profile")
    python_profile = profile["kind"] in {"python_test", "check"}
    if not python_profile and not config["dotnet"]:
        raise PilotError("Configure a local dotnet executable first", "missing_executable")
    args = profile.get("args", [])
    forbidden = {"--restore", "--interactive", "--force", "-restore", "/restore"}
    if any(
        a.lower() in forbidden or a.lower().startswith(("/t:", "-t:", "--target", "-p:restore", "/p:restore"))
        for a in args
    ):
        raise PilotError("Validation profiles cannot request restore/extra targets; use restore explicitly")
    snap = RepositoryIndex(ws).refresh()
    result_id = uid("validation")
    output = ws.state / "validation" / result_id
    output.mkdir(parents=True)
    if python_profile:
        if not profile.get("argv"):
            raise PilotError("Python/check profiles require a developer-authored argv array")
        command = [arg.replace("{results_dir}", str(output)) for arg in profile["argv"]]
    else:
        command = [config["dotnet"], profile["kind"], *args, "--no-restore"]
    runner = profile.get("runner", "auto")
    if profile["kind"] == "test":
        if runner == "auto":
            global_json = ws.root / "global.json"
            runner = (
                json.loads(global_json.read_text()).get("test", {}).get("runner", "VSTest")
                if global_json.exists()
                else "VSTest"
            )
        if runner == "VSTest":
            command += ["--logger", "trx", "--results-directory", str(output)]
        elif runner == "Microsoft.Testing.Platform":
            command += ["--report-trx", "--results-directory", str(output)]
        else:
            raise PilotError("Unknown test runner")
    result = run(command, ws.root, profile.get("timeout", 300), dotnet_env(config["dotnet"]))
    after = RepositoryIndex(ws).refresh()
    counters = []
    for trx in output.glob("**/*.trx"):
        if trx.stat().st_size > 10_000_000:
            continue
        try:
            tree = ET.parse(trx)
            counters.extend(dict(x.attrib) for x in tree.iter() if x.tag.endswith("Counters"))
        except ET.ParseError:
            continue
    if profile["kind"] == "python_test":
        runner = "JUnit"
        for report in output.glob("**/*.xml"):
            if report.stat().st_size > 10_000_000:
                continue
            try:
                cases = list(ET.parse(report).iter("testcase"))
                counters.append(
                    {
                        "executed": str(sum(c.find("skipped") is None for c in cases)),
                        "failed": str(sum(c.find("failure") is not None for c in cases)),
                        "error": str(sum(c.find("error") is not None for c in cases)),
                    }
                )
            except ET.ParseError:
                continue
    status = "passed" if result["exit_code"] == 0 else "failed"
    before_configuration = {
        k: v for k, v in snap["analysis_configuration"].items() if k != "generated_hashes"
    }
    after_configuration = {
        k: v for k, v in after["analysis_configuration"].items() if k != "generated_hashes"
    }
    source_changed = (
        after["hashes"] != snap["hashes"]
        or after["head"] != snap["head"]
        or before_configuration != after_configuration
    )
    if source_changed:
        status = "inconclusive_source_changed"
    if profile["kind"] in {"test", "python_test"} and status == "passed":
        executed = sum(int(c.get("executed", "0")) for c in counters)
        failed = sum(int(c.get("failed", "0")) + int(c.get("error", "0")) for c in counters)
        if failed:
            status = "failed"
        elif not executed:
            status = "inconclusive_no_test_evidence"
    record = {
        "id": result_id,
        "task_id": task_id,
        "at": now(),
        "snapshot": snap["id"] if source_changed else after["id"],
        "started_snapshot": snap["id"],
        "profile": profile_name,
        "profile_hash": digest(canonical(profile)),
        "status": status,
        "result": result,
        "test_counters": counters,
        "runner": runner,
        "evidence_kind": "tests" if profile["kind"] in {"test", "python_test"} else "check",
        "environment": {
            "dotnet": config["dotnet"] if not python_profile else None,
            "executable": command[0],
            "supervisor_python": sys.version,
            "platform": os.name,
            "offline_defaults": True,
        },
    }
    ws.put("validation", result_id, record)
    atomic_json(output / "result.json", record)
    if task_id:
        task = TaskStore(ws).get(task_id)
        task["validation"].append(result_id)
        if status != "passed":
            task["checkpoint_required"] = True
        TaskStore(ws).save(task)
    return record


class ValidationEvidence:
    """Retrieve concise validation summaries or bounded diagnostic lines by evidence ID."""

    def __init__(self, ws: Workspace):
        """Reuse locally persisted execution records without running processes."""
        self.ws = ws

    def inspect(
        self,
        snapshot: str,
        validation_id: str | None = None,
        start: int = 1,
        end: int = 80,
        legacy: bool = False,
        task_id: str | None = None,
    ):
        """Preserve legacy lists and provide managed diagnostic expansion with log citations."""
        if validation_id:
            record = self.ws.get("validation", validation_id)
            if task_id and record.get("task_id") not in {None, task_id}:
                raise PilotError("Validation belongs to another task", "wrong_task")
            lines = (
                record["result"].get("stdout", "") + "\n" + record["result"].get("stderr", "")
            ).splitlines()
            first = max(1, start)
            last = max(first, min(end, first + 499))
            return {
                "id": record["id"],
                "snapshot": record["snapshot"],
                "historical": self.historical(record, snapshot),
                "profile": record["profile"],
                "status": record["status"],
                "test_counters": record["test_counters"],
                "profile_hash": record["profile_hash"],
                "evidence_kind": record["evidence_kind"],
                "environment": record["environment"],
                "hash": digest(canonical(record["result"])),
                "path": f"validation/{record['id']}/result.json",
                "start": first,
                "end": min(last, len(lines)),
                "total_lines": len(lines),
                "text": "\n".join(lines[first - 1 : last]),
                "truncated": last < len(lines),
            }
        selected = [
            r for r in self.ws.records("validation") if not task_id or r.get("task_id") in {None, task_id}
        ]
        results = [
            {**r, "historical": self.historical(r, snapshot)}
            for r in sorted(selected, key=lambda r: r["at"])[-10:]
        ]
        if legacy:
            return results
        return [
            {
                **{k: v for k, v in r.items() if k != "result"},
                "exit_code": r["result"]["exit_code"],
                "diagnostics": "Use validation_results(validation_id, start, end) to expand captured evidence",
            }
            for r in results
        ]

    def historical(self, record: dict, snapshot: str) -> bool:
        """Treat evidence as historical after either source or the selected profile changes."""
        return record["snapshot"] != snapshot or record.get("profile_hash") != digest(
            canonical(self.ws.config["profiles"].get(record["profile"]))
        )
