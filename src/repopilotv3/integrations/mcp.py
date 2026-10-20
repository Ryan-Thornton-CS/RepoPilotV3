"""Standalone MCP setup plus four focused repository tools complementing Copilot's native development tools."""

from __future__ import annotations

from fastmcp import FastMCP

from repopilotv3.core import PilotError
from repopilotv3.repository.service import RepositoryTools
from repopilotv3.integrations.repositories import RepositoryBindings


def create_server(ws) -> FastMCP:
    """Build a discoverable server with bounded repository setup, retrieval and checkpoints."""
    mcp = FastMCP("RepoPilotV3")
    bindings = RepositoryBindings(ws)

    def selected(repository_id):
        """Resolve explicit roots and require setup only when a repository tool is executed."""
        workspace = bindings.select(repository_id)
        if not workspace.config_path.exists():
            raise PilotError("Call setup_repository to initialize this repository first", "not_initialized")
        return workspace, RepositoryTools(workspace)

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    def setup_repository(repository_path: str | None = None) -> dict:
        """Initialize private state for the configured root or an existing project subfolder; returns repository_id."""
        return bindings.setup(repository_path)

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def repository_status(repository_id: str | None = None) -> dict:
        """Inspect explicit repository identity, initialization and index availability without setup."""
        return bindings.status(repository_id)

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def search_repository(query: str, limit: int = 12, run_id: str | None = None, repository_id: str | None = None) -> dict:
        """Search short source keywords and available relationships; use '*' to list bounded paths."""
        ws, service = selected(repository_id)
        with ws.lock():
            return service.managed(
                "search", {"query": query, "limit": limit}, service.search(query, limit), run_id
            )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def read_source(path: str, start: int = 1, end: int = 200, run_id: str | None = None, repository_id: str | None = None) -> dict:
        """Retrieve 1–500 source lines with snapshot and SHA-256 citations."""
        ws, service = selected(repository_id)
        with ws.lock():
            return service.managed(
                "read", {"path": path, "start": start, "end": end}, service.read(path, start, end), run_id
            )

    @mcp.tool(annotations={"readOnlyHint": True, "openWorldHint": False})
    def get_context(query: str, paths: list[str] | None = None, run_id: str | None = None, repository_id: str | None = None) -> dict:
        """Return focused complete source units, symbols, relationships, and explicit omissions."""
        ws, service = selected(repository_id)
        with ws.lock():
            return service.managed(
                "context", {"query": query, "paths": paths}, service.context(query, paths), run_id
            )

    @mcp.tool(annotations={"readOnlyHint": False, "destructiveHint": False, "openWorldHint": False})
    def task_checkpoint(
        objective: str | None = None,
        task_id: str | None = None,
        progress: dict | None = None,
        seeds: list[str] | None = None,
        run_id: str | None = None,
        repository_id: str | None = None,
    ) -> dict:
        """Start/resume persistent TODOs; save unverified progress for a WebUI fallback."""
        ws, service = selected(repository_id)
        with ws.lock():
            saved = service.checkpoint(objective, task_id, progress, seeds, run_id)
            return service.managed(
                "task_checkpoint",
                {"task_id": task_id, "objective": objective, "progress": progress, "seeds": seeds},
                saved,
                saved["run_id"],
            )

    return mcp


def serve(ws) -> None:
    """Serve local MCP before repository initialization without any model API calls."""
    create_server(ws).run(transport="stdio", show_banner=False)
