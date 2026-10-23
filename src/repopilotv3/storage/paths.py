"""Resolve legacy and organized state paths without moving live files."""

from __future__ import annotations

import json
from pathlib import Path


class StatePaths:
    """Keep all growing private files in named folders while reading legacy state."""

    def __init__(self, root: Path):
        """Bind a private root; inspecting its layout never creates files."""
        self.root = root

    @property
    def legacy(self) -> bool:
        """Identify existing version-one configuration; new state uses version two."""
        config = self.root / "config.json"
        if not config.exists():
            return False
        return json.loads(config.read_text("utf-8")).get("schema_version", "1.0") == "1.0"

    def database(self, name: str) -> Path:
        """Resolve a database and let SQLite keep its sidecars in the same directory."""
        return self.root / name if self.legacy else self.root / "databases" / name

    def lock(self, name: str) -> Path:
        """Resolve writer and session leases without deleting live lock inodes."""
        return self.root / name if self.legacy else self.root / "locks" / name

    def runtime(self, name: str) -> Path:
        """Resolve small hook/status markers according to the published layout."""
        return self.root / name if self.legacy else self.root / "runtime" / name

    def ensure(self) -> None:
        """Create only the structured folders used by initialized version-two state."""
        if not self.legacy:
            for name in ("databases", "locks", "runtime", "browser", "recovery"):
                if (self.root / name).is_symlink():
                    from repopilotv3.core import PilotError
                    raise PilotError("Private state subfolders cannot be symlinks: " + name, "unsafe_path")
                (self.root / name).mkdir(parents=True, exist_ok=True)
