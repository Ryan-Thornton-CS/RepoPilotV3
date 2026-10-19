"""Four local MCP tools complementing Copilot's native development tools."""

from __future__ import annotations

from fastmcp import FastMCP

from repopilotv3.core import PilotError
from repopilotv3.repository.service import RepositoryTools


def create_server(ws) -> FastMCP:
    """Build a local stdio server with retrieval and checkpoint capabilities only."""
    mcp = FastMCP("RepoPilotV3")
    service = RepositoryTools(ws)

    @mcp.tool
    def search_repository(query: str, limit: int = 12, run_id: str | None = None) -> dict:
        """Search short source keywords and available relationships; use '*' to list bounded paths."""
        with ws.lock():
            return service.managed(
                "search", {"query": query, "limit": limit}, service.search(query, limit), run_id
            )

    @mcp.tool
    def read_source(path: str, start: int = 1, end: int = 200, run_id: str | None = None) -> dict:
        """Retrieve 1–500 source lines with snapshot and SHA-256 citations."""
        with ws.lock():
            return service.managed(
                "read", {"path": path, "start": start, "end": end}, service.read(path, start, end), run_id
            )

    @mcp.tool
    def get_context(query: str, paths: list[str] | None = None, run_id: str | None = None) -> dict:
        """Return focused complete source units, symbols, relationships, and explicit omissions."""
        with ws.lock():
            return service.managed(
                "context", {"query": query, "paths": paths}, service.context(query, paths), run_id
            )

    @mcp.tool
    def task_checkpoint(
        objective: str | None = None,
        task_id: str | None = None,
        progress: dict | None = None,
        seeds: list[str] | None = None,
        run_id: str | None = None,
    ) -> dict:
        """Start/resume persistent TODOs; save unverified progress for a WebUI fallback."""
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
    """Serve the four tools over local stdio without any model API calls."""
    if not ws.config_path.exists():
        raise PilotError("Run repopilotv3 setup first", "not_initialized")
    create_server(ws).run(transport="stdio", show_banner=False)
