"""Compare deterministic broad retrieval with focused tools using local synthetic content."""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import tempfile
import time
from pathlib import Path

from repopilotv3.core import Workspace, atomic_json, canonical, now
from repopilotv3.integrations.hooks import HookAdapter
from repopilotv3.integrations.monitor import Monitor
from repopilotv3.manual.sessions import measure
from repopilotv3.repository.service import RepositoryTools


def benchmark() -> dict:
    """Measure local calls/returned text and callback overhead, never model billing."""
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for index in range(40):
            name = "CriticalGreeter" if index == 7 else f"Component{index}"
            body = f'"""Synthetic {name} module."""\n\n\nclass {name}:\n    """Demonstration component."""\n'
            for method in range(15):
                body += f'\n    def operation_{method}(self):\n        """Return a fixture value."""\n        return {method}\n'
            (root / f"component_{index:02}.py").write_text(body)
        ws = Workspace(root)
        config = ws.initialize()
        config["dotnet"] = None
        atomic_json(ws.config_path, config)
        tools = RepositoryTools(ws)
        start = time.perf_counter()
        snap = tools.snapshot()
        cold = time.perf_counter() - start
        warm = []
        for _ in range(5):
            start = time.perf_counter()
            tools.snapshot()
            warm.append((time.perf_counter() - start) * 1000)
        context_times = []
        for _ in range(6):
            start = time.perf_counter()
            tools.context("CriticalGreeter", ["component_07.py"])
            context_times.append((time.perf_counter() - start) * 1000)
        baseline = [tools.index.source(path, snapshot=snap) for path in snap["hashes"]]
        baseline += [tools.index.source("component_07.py", snapshot=snap) for _ in range(3)]
        search = tools.search("CriticalGreeter")
        source = tools.read(search["hits"][0]["path"])
        checkpoint = tools.checkpoint(objective="Inspect CriticalGreeter")
        monitor = Monitor(ws)
        monitor.bind("benchmark-chat", checkpoint["run_id"])
        hook = HookAdapter(ws)
        overhead = []
        for sequence in range(30):
            start = time.perf_counter()
            hook.handle(
                "postToolUse",
                {
                    "sessionId": "benchmark-chat",
                    "timestamp": sequence,
                    "toolName": "Read",
                    "toolArgs": {"path": "component_07.py"},
                    "toolResult": {"text": "bounded result", "hash": "same-source"},
                },
            )
            overhead.append((time.perf_counter() - start) * 1000)
        return {
            "generated_at": now(),
            "environment": {"platform": platform.system(), "python": platform.python_version()},
            "scenario": "Find one uniquely named class among 40 synthetic files",
            "baseline": {
                "calls": len(baseline),
                "repeated_operations": 3,
                "returned_estimated_tokens": sum(measure(canonical(r))["estimated_tokens"] for r in baseline),
            },
            "focused": {
                "calls": 2,
                "repeated_operations": 0,
                "returned_estimated_tokens": sum(
                    measure(canonical(r))["estimated_tokens"] for r in [search, source]
                ),
            },
            "cache": {
                "cold_index_seconds": round(cold, 4),
                "warm_refresh_median_ms": round(statistics.median(warm), 3),
                "context_cold_ms": round(context_times[0], 3),
                "context_cached_median_ms": round(statistics.median(context_times[1:]), 3),
            },
            "hook_callback_median_ms": round(statistics.median(overhead), 3),
            "limits": "Local deterministic comparison only; hook timing excludes process startup and host dispatch. No billing or guaranteed model-token savings measured.",
        }


def main() -> None:
    """Print results or save a developer-selected JSON report."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    result = benchmark()
    if args.output:
        atomic_json(args.output, result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
