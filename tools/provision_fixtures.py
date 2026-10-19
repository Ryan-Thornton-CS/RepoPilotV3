"""Explicitly provision synthetic SDK/MSTest/framework fixture dependencies.

This developer-invoked operation may access configured NuGet feeds. RepoPilot's
normal index/export/build/test commands never restore implicitly. No C# source
is authored as implementation: generated C# files below are disposable test data.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

from repopilotv3.core import atomic_json, dotnet_env


def provision(dotnet: str, destination: Path) -> None:
    """Create and explicitly restore modern, framework, and synthetic interop fixtures."""
    spec = importlib.util.spec_from_file_location(
        "create_demo", Path(__file__).parents[1] / "tools/create_csharp_fixture.py"
    )
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    for name, framework, inventor in [
        ("modern", "net10.0", False),
        ("framework", "net48", False),
        ("inventor", "net48", True),
    ]:
        root = destination / name
        if not (root / "Demo.csproj").exists():
            demo.create(root, framework, with_tests=True, inventor=inventor)
        projects = (
            ["interop/Interop.csproj", "Demo.csproj", "Tests/Calculator.Tests.csproj"]
            if inventor
            else ["Demo.csproj", "Tests/Calculator.Tests.csproj"]
        )
        for project in projects:
            subprocess.run(
                [dotnet, "restore", project, "--use-lock-file"], cwd=root, env=dotnet_env(dotnet), check=True
            )
        if inventor:
            subprocess.run(
                [dotnet, "build", "interop/Interop.csproj", "--no-restore"],
                cwd=root,
                env=dotnet_env(dotnet),
                check=True,
            )
        atomic_json(
            root / "fixture.json", {"framework": framework, "inventor_stub": inventor, "provisioned": True}
        )
    print(json.dumps({"fixtures": str(destination.resolve())}))


def main() -> None:
    """Require an explicit SDK path and optional fixture output directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dotnet", required=True)
    parser.add_argument("--destination", type=Path, default=Path(".tools/fixtures"))
    args = parser.parse_args()
    executable = shutil.which(args.dotnet) or str(Path(args.dotnet).resolve())
    provision(executable, args.destination.resolve())


if __name__ == "__main__":
    main()
