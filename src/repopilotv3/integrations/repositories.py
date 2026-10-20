"""Explicit repository bindings within one MCP server's locally authorized root."""

from __future__ import annotations

from pathlib import Path
from threading import RLock

from repopilotv3.core import PilotError, Workspace, digest
from repopilotv3.repository.index import SKIP_DIRS


class RepositoryBindings:
    """Keep subrepository identities independent instead of changing a shared active root."""

    def __init__(self, workspace: Workspace):
        """Retain the startup root as the default and repository authorization boundary."""
        self.default = workspace
        self.initializing = RLock()
        self.bound = {self.identity(workspace): workspace}

    @staticmethod
    def identity(workspace: Workspace) -> str:
        """Derive an opaque stable identifier from canonical repository and state paths."""
        return 'repo-' + digest(str(workspace.root) + '\0' + str(workspace.state))[:24]

    def select(self, repository_id: str | None = None) -> Workspace:
        """Resolve a previously bound identifier without altering another caller's choice."""
        if repository_id is None:
            return self.default
        if repository_id not in self.bound:
            raise PilotError('Unknown repository_id; call setup_repository with its project path first', 'unknown_repository')
        return self.bound[repository_id]

    def setup(self, repository_path: str | None = None) -> dict:
        """Initialize only private state in an existing authorized project directory."""
        path = Path(repository_path).expanduser() if repository_path else self.default.root
        if not path.is_absolute():
            path = self.default.root / path
        path = path.resolve()
        if not path.is_relative_to(self.default.root) or not path.is_dir():
            raise PilotError('Choose an existing project inside the configured MCP root', 'unsafe_repository')
        if path != self.default.root and (path.is_relative_to(self.default.state) or any(part.casefold() in {p.casefold() for p in SKIP_DIRS} for part in path.relative_to(self.default.root).parts)):
            raise PilotError('Private/generated directories cannot be selected as repositories', 'unsafe_repository')
        workspace = self.default if path == self.default.root else Workspace(path)
        with self.initializing:
            workspace.initialize()
        key = self.identity(workspace)
        self.bound[key] = workspace
        result = self.status(key)
        result.update(harness_written=False, validation_run=False, dependencies_installed=False)
        # No migrations, model calls or workspace harness writes occur here.
        return result

    def status(self, repository_id: str | None = None) -> dict:
        """Describe initialization and indexing without creating state or invoking a toolchain."""
        workspace = self.select(repository_id)
        initialized = workspace.config_path.exists()
        result = {'repository_id': self.identity(workspace), 'repository_path': str(workspace.root),
                  'state_path': str(workspace.state), 'initialized': initialized,
                  'authorized_root': str(self.default.root), 'indexed': False,
                  'next': 'Use retrieval tools.' if initialized else 'Call setup_repository before repository tools.'}
        if initialized:
            workspace.config
            with workspace.db() as db:
                row = db.execute("SELECT value FROM records WHERE kind='meta' AND id='current_snapshot'").fetchone()
            result['indexed'] = row is not None
        return result
