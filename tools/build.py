"""Build one offline-capable VSIX with locked Windows and Linux Python runtimes.

Run explicitly with ``uv run --group dev python tools/build.py``. Build-time
network access provisions verified CPython archives and pinned dependency wheels;
the installed extension performs no environment creation or dependency download.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement


class BundleBuilder:
    """Stage relocatable Python runtimes, backend services, static harness and docs."""

    def __init__(self, root: Path, offline: bool = False):
        """Keep all generated binaries in ignored build/staging directories."""
        self.root = root
        self.cache = root / ".tools/bundle"
        self.extension = root / "extension"
        self.offline = offline
        self.lock = json.loads((root / "tools/runtime-lock.json").read_text())
        self.uv = shutil.which("uv")
        self.npm = shutil.which("npm")
        if not self.uv or not self.npm:
            raise RuntimeError("Build tools uv and Node/npm must be available on PATH.")

    def run(self, arguments: list[str], capture: bool = False, cwd: Path | None = None):
        """Execute a build command as an argument array and stop on its first failure."""
        result = subprocess.run(
            arguments,
            cwd=cwd or self.root,
            check=True,
            capture_output=capture,
            text=True,
            encoding="utf-8",
            env={**os.environ, "PYTHONUTF8": "1"},
        )
        return result.stdout if capture else None

    def download(self, platform: str, item: dict) -> Path:
        """Use pinned uv's verified standalone-Python metadata and isolated build cache."""
        version = self.run([self.uv, "--version"], capture=True)
        if not version.startswith("uv " + self.lock["uv_version"] + " "):
            raise RuntimeError("Reproducible runtime builds require uv " + self.lock["uv_version"])
        root = self.cache / "python"
        arguments = [self.uv, "python", "install", "--no-bin", "--no-registry", "--install-dir", str(root)]
        if self.offline:
            arguments.append("--offline")
        arguments.append(item["key"])
        self.run(arguments)
        return root / item["key"]

    def extract(self, source: Path, destination: Path) -> None:
        """Copy uv's verified runtime, materializing links for portable extension ZIPs."""
        def ignored(directory: str, names: list[str]) -> list[str]:
            """Skip unused dangling aliases while keeping real internal runtime links."""
            omitted = []
            for name in names:
                path = Path(directory) / name
                if path.is_symlink():
                    if not path.exists():
                        omitted.append(name)
                    elif not path.resolve().is_relative_to(source.resolve()):
                        raise RuntimeError("Runtime symlink escapes its verified tree: " + str(path))
            return omitted

        shutil.copytree(source, destination, symlinks=False, ignore=ignored)

    def target_requirements(self, platform: str, requirements: Path) -> Path:
        """Evaluate locked markers for the target rather than the build host's operating system."""
        environment = default_environment()
        environment.update(
            python_version="3.12",
            python_full_version="3.12.14",
            implementation_version="3.12.14",
            platform_python_implementation="CPython",
            implementation_name="cpython",
            platform_machine="AMD64" if platform == "win32-x64" else "x86_64",
            os_name="nt" if platform == "win32-x64" else "posix",
            sys_platform="win32" if platform == "win32-x64" else "linux",
            platform_system="Windows" if platform == "win32-x64" else "Linux",
            extra="",
        )
        active, pending = [], ""
        for line in requirements.read_text().splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            pending += line.rstrip().removesuffix("\\").strip() + " "
            if line.rstrip().endswith("\\"):
                continue
            text, separator, hashes = pending.strip().partition(" --hash=")
            requirement = Requirement(text)
            if not requirement.marker or requirement.marker.evaluate(environment):
                active.append(str(requirement).split(";", 1)[0].strip() + separator + hashes)
            pending = ""
        destination = self.cache / f"requirements-{platform}.txt"
        destination.write_text("\n".join(active) + "\n")
        return destination

    def wheelhouse(self, platform: str, item: dict, requirements: Path) -> Path:
        """Download hash-locked binary wheels for the target without executing target Python."""
        requirements = self.target_requirements(platform, requirements)
        wheels = self.cache / "wheels" / platform
        wheels.mkdir(parents=True, exist_ok=True)
        pip = [
            str(self.cache / "build-python" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")),
            "-m",
            "pip",
        ]
        feeds = ["--no-index", "--find-links", str(wheels)] if self.offline else []
        self.run(
            [
                *pip,
                "download",
                "--no-deps",
                *feeds,
                "--only-binary=:all:",
                "--require-hashes",
                "--platform",
                item["wheel_platform"],
                *(["--platform", "manylinux2014_x86_64"] if platform == "linux-x64" else []),
                "--python-version",
                "3.12",
                "--implementation",
                "cp",
                "--abi",
                "cp312",
                "--dest",
                str(wheels),
                "-r",
                str(requirements),
            ]
        )
        return wheels

    def runtime(self, platform: str, item: dict, requirements: Path, wheel: Path) -> dict:
        """Produce one runnable, source-versioned runtime without a first-run installer."""
        fingerprint = hashlib.sha256(
            wheel.read_bytes() + requirements.read_bytes() + json.dumps(item, sort_keys=True).encode()
            + Path(__file__).read_bytes()
        ).hexdigest()
        cached = self.cache / "runtimes" / platform / fingerprint
        marker = cached / "complete.json"
        if not marker.exists():
            shutil.rmtree(cached, ignore_errors=True)
            cached.mkdir(parents=True)
            runtime = cached / "python"
            self.extract(self.download(platform, item), runtime)
            packages = runtime / item["site_packages"]
            packages.mkdir(parents=True, exist_ok=True)
            wheels = self.wheelhouse(platform, item, requirements)
            target_requirements = self.target_requirements(platform, requirements)
            pip = [
                str(
                    self.cache / "build-python" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
                ),
                "-m",
                "pip",
            ]
            self.run(
                [
                    *pip,
                    "install",
                    "--no-deps",
                    "--no-index",
                    "--find-links",
                    str(wheels),
                    "--only-binary=:all:",
                    "--require-hashes",
                    "--no-compile",
                    "--platform",
                    item["wheel_platform"],
                    *(["--platform", "manylinux2014_x86_64"] if platform == "linux-x64" else []),
                    "--python-version",
                    "3.12",
                    "--implementation",
                    "cp",
                    "--abi",
                    "cp312",
                    "--target",
                    str(packages),
                    "-r",
                    str(target_requirements),
                ]
            )
            # Backend is pure Python; extract the built wheel after validating all dependency wheels.
            import zipfile

            with zipfile.ZipFile(wheel) as package:
                package.extractall(packages)
            # The static harness lives once at extension/harness, not twice inside each runtime.
            shutil.rmtree(packages / "repopilotv3/harness", ignore_errors=True)
            for directory in sorted(runtime.rglob("__pycache__"), reverse=True):
                shutil.rmtree(directory)
            # Headers and packaging tools are build inputs, not runtime functionality.
            shutil.rmtree(runtime / "include", ignore_errors=True)
            shutil.rmtree(runtime / "share/man", ignore_errors=True)
            # No terminal UI is shipped; terminfo also contains VSIX-invalid case-only names.
            shutil.rmtree(runtime / "share/terminfo", ignore_errors=True)
            # All managed commands use python -m; avoid duplicate materialized interpreter aliases
            # and generated console launchers with build-host-specific absolute shebangs.
            if platform == "linux-x64":
                for launcher in (runtime / "bin").iterdir():
                    if launcher.name != "python3.12":
                        launcher.unlink()
            else:
                shutil.rmtree(runtime / "Scripts", ignore_errors=True)
            metadata = {
                "python": f"{platform}/python/{item['python']}",
                "python_version": item["version"],
                "archive_sha256": item["sha256"],
                "fingerprint": fingerprint,
            }
            marker.write_text(json.dumps(metadata, indent=2) + "\n")
        metadata = json.loads(marker.read_text())
        destination = self.extension / "backend" / platform
        shutil.rmtree(destination, ignore_errors=True)
        shutil.copytree(cached / "python", destination / "python")
        return metadata

    def build(self) -> Path:
        """Build the backend internally, stage both supported hosts, and emit only the VSIX."""
        self.cache.mkdir(parents=True, exist_ok=True)
        requirements = self.cache / "requirements.txt"
        self.run(
            [
                self.uv,
                "export",
                "--frozen",
                "--no-dev",
                "--group",
                "bundle",
                "--extra",
                "csharp",
                "--no-emit-project",
                "--output-file",
                str(requirements),
                "--quiet",
            ],
            capture=True,
        )
        build_python = (
            self.cache / "build-python" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        )
        if not build_python.exists():
            self.run([sys.executable, "-m", "venv", str(self.cache / "build-python")])
        backend_wheels = self.cache / "backend-wheel"
        backend_wheels.mkdir(exist_ok=True)
        build = [self.uv, "build", "--wheel", "--out-dir", str(backend_wheels)]
        if self.offline:
            build.append("--offline")
        self.run(build)
        wheel = backend_wheels / "repopilotv3-0.2.10-py3-none-any.whl"
        shutil.rmtree(self.extension / "backend", ignore_errors=True)
        (self.extension / "backend").mkdir()
        manifest = {"schema_version": "1.0", "backend_version": "0.2.10", "platforms": {}}
        for platform, item in self.lock["platforms"].items():
            print("Staging " + platform, flush=True)
            manifest["platforms"][platform] = self.runtime(platform, item, requirements, wheel)
        (self.extension / "backend/manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        for name in ["README.md", "LICENSE"]:
            shutil.copyfile(self.root / name, self.extension / name)
        for name in ["harness", "docs"]:
            shutil.rmtree(self.extension / name, ignore_errors=True)
            shutil.copytree(self.root / name, self.extension / name)
        shutil.rmtree(self.extension / "out", ignore_errors=True)
        self.run([self.npm, "run", "compile"], cwd=self.extension)
        (self.root / "dist").mkdir(exist_ok=True)
        result = self.root / "dist/repopilotv3-0.2.10.vsix"
        node = shutil.which("node")
        if not node:
            raise RuntimeError("Node.js is required only for building the extension.")
        self.run(
            [
                node,
                str(self.extension / "node_modules/@vscode/vsce/vsce"),
                "package",
                "--no-dependencies",
                "--allow-missing-repository",
                "--no-rewrite-relative-links",
                "--out",
                str(result),
            ],
            cwd=self.extension,
        )
        return result


def main() -> None:
    """Expose one developer build command with an optional fully cached offline mode."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    print(BundleBuilder(Path(__file__).parents[1], args.offline).build())


if __name__ == "__main__":
    main()
