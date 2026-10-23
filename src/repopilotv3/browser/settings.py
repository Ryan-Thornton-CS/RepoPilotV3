"""Private browser mappings and idempotent transfer evidence, separate from model tools."""

from __future__ import annotations

from urllib.parse import urlsplit

from repopilotv3.core import PilotError, atomic_json, digest, load_json, now
from repopilotv3.manual.sessions import session_operation


class BrowserSettings:
    """Persist project defaults and explicit per-chat overrides without contacting URLs."""

    def __init__(self, workflow):
        """Reuse existing session ownership checks and observed-transfer accounting."""
        self.workflow = workflow
        self.ws = workflow.ws

    def configure(self, session_id: str | None = None, settings: dict | None = None) -> dict:
        """Read or merge approved local settings; restoring them never starts automation."""
        if session_id:
            self.workflow.check(session_id)
        path = self.ws.state / "browser" / (session_id or "project") / "settings.json"
        current = load_json(path, {})
        if settings is not None:
            url = settings.get("url")
            if url:
                try:
                    parsed = urlsplit(url)
                    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or any(c.isspace() for c in url):
                        raise ValueError("Use an HTTP/HTTPS URL without embedded credentials or whitespace")
                    parsed.port
                except ValueError as exc:
                    raise PilotError(str(exc), "invalid_url") from exc
            if url is not None and urlsplit(url).netloc != urlsplit(current.get("url", "")).netloc and "selectors" not in settings:
                current.pop("selectors", None)
            current.update(settings)
            atomic_json(path, current)
        defaults = load_json(self.ws.state / "browser/project/settings.json", {})
        effective = {"enabled": False, "mode": "fill", "budget": 10, "url": "", "selectors": {}, **defaults, **current}
        if session_id and not current.get("url"):
            effective["url"] = defaults.get("url", "")
        parsed = urlsplit(effective["url"])
        origin = parsed.scheme + "://" + parsed.netloc
        site_path = self.ws.state / "browser" / "sites" / digest(origin) / "settings.json"
        if settings and "selectors" in settings and effective["url"]:
            atomic_json(site_path, {"origin": origin, "selectors": settings["selectors"]})
        if "selectors" not in current and effective["url"]:
            effective["selectors"] = load_json(site_path, {}).get("selectors", effective["selectors"])
        return {"settings": effective, "local": current, "armed": False}

    @session_operation
    def transfer(self, session_id: str, transfer_id: str, phase: str, text: str = "") -> dict:
        """Journal transitions; count acknowledged sends once, not generated or filled text."""
        self.workflow.check(session_id)
        key = session_id + ":" + transfer_id
        try:
            previous = self.ws.get("browser_transfer", key)
        except PilotError as exc:
            if exc.code != "not_found":
                raise
            previous = {}
        if text and previous.get("hash") and previous["hash"] != digest(text):
            raise PilotError("Transfer identity already belongs to different content", "request_conflict")
        if phase == "inspect":
            return previous
        if previous.get("phase") in {"sent", "imported"}:
            return previous
        record = {**previous, "session_id": session_id, "transfer_id": transfer_id,
                  "phase": phase, "at": now()}
        if text:
            record.update(text=text, hash=digest(text))
        if phase == "sent":
            self.workflow.transfer(session_id, record.get("text", ""))
        self.ws.put("browser_transfer", key, record)
        return record
