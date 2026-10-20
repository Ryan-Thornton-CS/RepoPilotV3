"""Create and verify a source-plus-VSIX distribution without publishing externally."""

from __future__ import annotations

import hashlib
import json
import tomllib
import zipfile
from pathlib import Path


class ReleaseArchive:
    """Assemble explicit public inputs only; never scan private state or build caches."""

    def __init__(self, root: Path):
        """Read the synchronized release version from project metadata."""
        self.root = root
        self.version = tomllib.loads((root / "pyproject.toml").read_text("utf-8"))["project"]["version"]
        self.prefix = f"repopilotv3-{self.version}/"

    def inputs(self) -> dict[str, bytes]:
        """Collect the complete backend sources, lockfiles and manually usable workspace harness."""
        result = {}
        for directory in ["src", "harness"]:
            for source in sorted((self.root / directory).rglob("*")):
                if source.is_file() and not source.is_symlink() and "__pycache__" not in source.parts:
                    result["backend/" + source.relative_to(self.root).as_posix()] = source.read_bytes()
        for name in ["pyproject.toml", "uv.lock", "README.md", "LICENSE"]:
            result["backend/" + name] = (self.root / name).read_bytes()
        for source in sorted((self.root / "harness/workspace").rglob("*")):
            if source.is_file():
                result[source.relative_to(self.root / "harness/workspace").as_posix()] = source.read_bytes()
        result["alternatives/.vscode/mcp.json"] = (self.root / "harness/alternatives/.vscode/mcp.json").read_bytes()
        result["LICENSE"] = (self.root / "LICENSE").read_bytes()
        result["README.md"] = (
            f"# RepoPilotV3 {self.version}\n\n"
            "Install the included VSIX using VS Code: Extensions → Install from VSIX. "
            "It contains the complete Windows/Linux Python runtimes and backend; no Python setup is needed.\n\n"
            "The loose .github harness and MCP examples are optional. Merge them manually or use "
            "RepoPilot's previewed setup actions. Configure executable paths before using standalone MCP. "
            "See backend/harness/README.md. backend/ contains the complete Python source and locked dependency metadata. "
            "Building/installing that source requires an explicit local dependency-provisioning action.\n\n"
            "SHA256SUMS lists every payload file. The adjacent outer SHA256SUMS also covers this ZIP and the VSIX.\n"
        ).encode("utf-8")
        return result

    def build(self) -> Path:
        """Write a versioned ZIP with internal checksums and verify its exact payload."""
        vsix = self.root / f"dist/repopilotv3-{self.version}.vsix"
        with zipfile.ZipFile(vsix) as source:
            package = json.loads(source.read("extension/package.json"))
            manifest = json.loads(source.read("extension/backend/manifest.json"))
            if package["version"] != self.version or manifest["backend_version"] != self.version:
                raise ValueError("VSIX/backend versions must match source metadata")
        payload = self.inputs()
        payload[vsix.name] = vsix.read_bytes()
        sums = "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in sorted(payload.items()))
        payload["SHA256SUMS"] = sums.encode("utf-8")
        target = vsix.with_name(f"repopilotv3-{self.version}-release.zip")
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, data in sorted(payload.items()):
                archive.writestr(self.prefix + name, data, compress_type=zipfile.ZIP_STORED if name.endswith(".vsix") else zipfile.ZIP_DEFLATED)
        self.verify(target, payload)
        (target.parent / "SHA256SUMS").write_text("".join(
            f"{hashlib.sha256(item.read_bytes()).hexdigest()}  {item.name}\n" for item in [vsix, target]), "utf-8")
        return target

    def verify(self, target: Path, payload: dict[str, bytes]) -> None:
        """Check member equality and every checksum before a distribution can be uploaded."""
        with zipfile.ZipFile(target) as archive:
            if set(archive.namelist()) != {self.prefix + name for name in payload}:
                raise ValueError("Distribution members differ from explicit public inputs")
            for name, data in payload.items():
                if archive.read(self.prefix + name) != data:
                    raise ValueError("Distribution content differs: " + name)


def main() -> None:
    """Build a distribution from the already provisioned and verified local VSIX."""
    print(ReleaseArchive(Path(__file__).parents[1]).build())


if __name__ == "__main__":
    main()
