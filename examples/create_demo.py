"""Create a disposable Python repository for the complete offline manual walkthrough."""

from __future__ import annotations

import argparse
from pathlib import Path


FILES = {
    "greeter.py": '''"""Small synthetic greeting module."""


class Greeter:
    """Build a greeting for one person."""

    def greet(self, name: str) -> str:
        """Return the current greeting behavior."""
        return f"Hello {name}"
''',
    "test_greeter.py": '''"""Synthetic regression test used by the walkthrough."""

from greeter import Greeter


def test_greeting():
    """Require the greeting's intended punctuation."""
    assert Greeter().greet("Ada") == "Hello, Ada!"
''',
    "README.md": "# Synthetic Greeter\n\nFix the greeting punctuation and verify the regression test.\n",
    ".gitignore": ".repopilotv3/\n__pycache__/\n.pytest_cache/\n",
}


def create(destination: Path) -> Path:
    """Create fixture files without overwriting an existing project."""
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ValueError("Choose an empty destination")
    for name, body in FILES.items():
        (destination / name).write_text(body, encoding="utf-8")
    return destination


def main() -> None:
    """Write the synthetic project to a developer-selected directory."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    print(create(parser.parse_args().destination).resolve())


if __name__ == "__main__":
    main()
