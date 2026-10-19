"""Size-triggered retention for generated exports and their unused artifact cache only."""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from repopilotv3.core import PilotError, atomic_json, canonical, digest, load_json
from repopilotv3.manual.sessions import SessionStore

GENERATION = re.compile(r"generation-[0-9a-f]{16}")
CACHE_FILE = re.compile(r"[0-9a-f]{64}\.(?:data|json)")


class ExportCleanup:
    """Reclaim oldest superseded packages under the caller's repository writer lock."""

    def __init__(self, ws):
        """Restrict retention to the workspace's generated export directory."""
        self.ws, self.base = ws, ws.state / "exports"
        self.report_path = self.base / "cleanup.json"

    @staticmethod
    def size(path) -> int:
        """Measure ordinary generated files without following symlinks."""
        if path.is_symlink():
            raise PilotError("Export cleanup skipped a symlink", "unsafe_path")
        if path.is_file():
            return path.stat().st_size
        total = 0
        for directory, dirs, files in os.walk(path, followlinks=False):
            for name in dirs + files:
                item = Path(directory) / name
                if item.is_symlink():
                    raise PilotError("Export cleanup skipped a symlink", "unsafe_path")
                if item.is_file():
                    total += item.stat().st_size
        return total

    def run(self, force: bool = False) -> dict:
        """Throttle automatic scans and keep cleanup failures independent of successful publication."""
        policy = self.ws.config["export_cleanup"]
        if not policy["enabled"]:
            return {"enabled": False}
        if self.base.is_symlink() or (self.base / "artifacts").is_symlink() or self.report_path.is_symlink():
            return {"enabled": True, "status": "degraded", "error": "Export cleanup refuses linked storage paths"}
        try:
            previous = load_json(self.report_path, {})
            fingerprint = digest(canonical(policy))
            if not force and previous.get("policy") == fingerprint and time.time() - previous.get("checked_at", 0) < policy["interval_seconds"]:
                return previous
            report = self.collect(policy)
            report.update(enabled=True, policy=fingerprint, checked_at=time.time())
        except (OSError, ValueError, TypeError, KeyError, PilotError) as exc:
            report = {"enabled": True, "status": "degraded", "error": str(exc), "checked_at": time.time()}
        try:
            atomic_json(self.report_path, report)
        except OSError:
            pass
        return report

    def collect(self, policy: dict) -> dict:
        """Mark current/recent generations and sweep only unreachable generated data under size pressure."""
        generations = {}
        for path in self.base.glob("generation-*"):
            if GENERATION.fullmatch(path.name):
                generations[path.name] = {"path": path, "size": self.size(path), "time": path.stat().st_mtime}
        cache = self.base / "artifacts"
        cached = {path: self.size(path) for path in cache.glob("*") if CACHE_FILE.fullmatch(path.name)}
        before = sum(g["size"] for g in generations.values()) + sum(cached.values())
        report = {"status": "ok", "before_bytes": before, "remaining_bytes": before, "freed_bytes": 0,
                  "limit_bytes": policy["max_bytes"], "target_bytes": policy["target_bytes"],
                  "removed_generations": 0, "removed_artifacts": 0, "over_limit": False}
        if before <= policy["max_bytes"]:
            return report
        protected, by_session, references, current_hashes = set(), {}, {}, set()
        sessions = SessionStore(self.ws)
        for path in self.base.glob("current-*.json"):
            if path.is_symlink():
                raise PilotError("Linked export pointer; cleanup skipped", "unsafe_path")
            pointer = load_json(path)
            try:
                sessions.get(pointer["session_id"])
            except PilotError as exc:
                if exc.code not in {"not_found", "session_deleted"}:
                    raise
            else:
                protected.add(pointer["id"])
                current_hashes.update(artifact["hash"] for artifact in pointer["artifacts"].values())
        for key, generation in generations.items():
            manifest_path = generation["path"] / "manifest.json"
            manifest = load_json(manifest_path) if manifest_path.exists() else None
            if manifest is None:
                # An unfinished recent generation may belong to an interrupted or external publisher.
                if time.time() - generation["time"] < 3600:
                    protected.add(key)
                references[key] = set()
                continue
            if manifest["id"] != key:
                raise PilotError("Inconsistent export manifest; cleanup skipped", "artifact_corrupt")
            generation["time"] = datetime.fromisoformat(manifest["created_at"]).timestamp()
            references[key] = {artifact["hash"] for artifact in manifest["artifacts"].values()}
            try:
                sessions.get(manifest["session_id"])
            except PilotError as exc:
                if exc.code not in {"not_found", "session_deleted"}:
                    raise
            else:
                by_session.setdefault(manifest["session_id"], []).append(key)
        for keys in by_session.values():
            protected.update(sorted(keys, key=lambda key: generations[key]["time"], reverse=True)[:policy["keep_generations"]])
        # Cache metadata is examined before deletion, so malformed metadata fails conservatively.
        cache_hashes = {}
        for path in cached:
            metadata = path.with_suffix(".json")
            info = load_json(metadata) if metadata.exists() else None
            cache_hashes[path] = info["hash"] if info else None
        retained = set(generations)

        def sweep_cache():
            """Drop unused cache pairs while preserving every remaining generation's artifact hashes."""
            used = current_hashes.union(*(references[key] for key in retained))
            # Remove metadata first: interruption can leave harmless orphan data, never a cache
            # hit whose .json survives while its .data has already disappeared.
            for path in sorted(cached, key=lambda item: (item.suffix != ".json", item.name)):
                if cache_hashes[path] not in used:
                    path.unlink(missing_ok=True)
                    report["remaining_bytes"] -= cached.pop(path)
                    report["removed_artifacts"] += 1

        sweep_cache()
        for key in sorted(generations, key=lambda name: generations[name]["time"]):
            if report["remaining_bytes"] <= policy["target_bytes"]:
                break
            if key in protected:
                continue
            shutil.rmtree(generations[key]["path"])
            retained.remove(key)
            report["remaining_bytes"] -= generations[key]["size"]
            report["removed_generations"] += 1
            sweep_cache()
        report["freed_bytes"] = before - report["remaining_bytes"]
        report["over_limit"] = report["remaining_bytes"] > policy["max_bytes"]
        if report["over_limit"]:
            report["warning"] = "Current/recent packages exceed the soft limit; protected data was retained."
        return report
