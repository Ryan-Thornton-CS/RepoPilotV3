"""Group durable chat events and copy a complete turn independently of history pagination."""

from __future__ import annotations

import json

from repopilotv3.core import PilotError, canonical


class ConversationTurns:
    """Render one copyable user/model/harness turn from its underlying saved events."""

    def __init__(self, history):
        """Reuse the independent history database without indexing or executing requests."""
        self.history = history

    @staticmethod
    def role(entry: dict) -> str:
        """Keep forwarded developer prompts with their user message and tools with the harness."""
        if entry["kind"] in {"user", "note"} or entry["body"].get("action") == "forward":
            return "user"
        return "model" if entry["kind"] == "model" else "harness"

    @staticmethod
    def starts(entry: dict) -> bool:
        """Separate incoming messages and explicit settings actions into their own turns."""
        return entry["kind"] in {"user", "note", "model"} or (
            entry["kind"] == "local" and entry["body"].get("action") == "session_settings"
        )

    @classmethod
    def decode(cls, row) -> dict:
        """Decode stored data and attach presentation-only turn hints without changing history."""
        entry = {**dict(row), "body": json.loads(row["body"])}
        return {**entry, "turn_role": cls.role(entry), "turn_start": cls.starts(entry)}

    @classmethod
    def separate(cls, previous: dict, current: dict) -> bool:
        """Determine a boundary using the same hints supplied to the webview."""
        return cls.role(previous) != cls.role(current) or cls.starts(current)

    def collect(self, session: str, entry_id: str) -> list[dict]:
        """Read both sides of an anchor so a paged or restored panel still copies the full turn."""
        with self.history.db() as db:
            db.execute("BEGIN")
            row = db.execute("SELECT * FROM entries WHERE session=? AND id=?", (session, entry_id)).fetchone()
            if row is None:
                raise PilotError("Turn is unavailable; refresh this chat's history before copying.", "not_found")
            anchor = self.decode(row)
            before, after = [], []
            size = len(row["body"].encode("utf-8"))
            for direction, target in [("before", before), ("after", after)]:
                current = anchor
                comparison, order = ("<", "DESC") if direction == "before" else (">", "ASC")
                while True:
                    row = db.execute(
                        f"SELECT * FROM entries WHERE session=? AND sequence{comparison}? ORDER BY sequence {order} LIMIT 1",
                        (session, current["sequence"]),
                    ).fetchone()
                    if row is None:
                        break
                    candidate = self.decode(row)
                    previous, following = (candidate, current) if direction == "before" else (current, candidate)
                    if self.separate(previous, following):
                        break
                    size += len(row["body"].encode("utf-8"))
                    if size > 32 * 1024 * 1024:
                        raise PilotError("This turn exceeds the 32 MB copy limit. Retrieve specific results from its saved outbox files.", "turn_too_large")
                    target.append(candidate)
                    current = candidate
        return [*reversed(before), anchor, *after]

    def copy(self, session: str, entry_id: str) -> dict:
        """Copy every saved reply/error/result once, retaining statuses and unexecuted request IDs."""
        entries = self.collect(session, entry_id)
        role = self.role(entries[0])
        if role == "user":
            forwarded = [entry for entry in entries if entry["body"].get("action") == "forward"]
            selected = forwarded[-1:] or entries
            text = "\n\n".join(entry["body"].get("copy_text", entry["body"].get("text", "")) for entry in selected)
        elif role == "model":
            text = "\n\n".join(entry["body"].get("raw", entry["body"].get("text", "")) for entry in entries)
        elif (all(entry["kind"] in {"harness", "local"} and not entry["body"].get("error")
                  and not str(entry["body"].get("copy_text", "")).startswith("{") for entry in entries)
              and any(entry["body"].get("text") for entry in entries)):
            text = "\n\n".join(entry["body"].get("copy_text", entry["body"].get("text", ""))
                                for entry in entries if entry["body"].get("copy_text") or entry["body"].get("text"))
        else:
            from repopilotv3.workflow.formatting import ReplyFormatter

            text, payload = ReplyFormatter().render(self.packet(session, entries))
            return {"session_id": session, "entry_ids": [entry["id"] for entry in entries],
                    "role": role, "copy_text": text, "copy_data": payload}
        return {"session_id": session, "entry_ids": [entry["id"] for entry in entries],
                "role": role, "copy_text": text}

    def packet(self, session: str, entries: list[dict]) -> dict:
        """Prefer authoritative final operation records and keep independent local errors separate."""
        packet = {"protocol_version": "1.0", "session_id": session, "results": []}
        covered, seen_replies, metadata, messages, errors, actions = set(), set(), {}, [], [], []
        aggregate_results = []
        for entry in entries:
            body = entry["body"]
            saved = body.get("copy_text", "")
            if isinstance(saved, str) and saved.startswith("{"):
                try:
                    aggregate = json.loads(saved)
                except ValueError:
                    aggregate = {}
                if isinstance(aggregate, dict):
                    aggregate_results.extend(aggregate.get("results", []))
                    for key in ("status", "remaining_request_ids", "pending", "advisories", "warnings", "next", "error"):
                        if key in aggregate:
                            metadata[key] = aggregate[key]
            reply_id = body.get("reply_id")
            if reply_id and reply_id not in seen_replies:
                seen_replies.add(reply_id)
                try:
                    record = self.history.ws.get("reply", session + ":" + reply_id)
                except PilotError as exc:
                    if exc.code != "not_found":
                        raise
                else:
                    packet["results"].extend(record["results"])
                    covered.update(result.get("request_id") for result in record["results"] if result.get("request_id"))
                    metadata.update(status=record["status"], pending=record["pending"],
                                    remaining_request_ids=[request["request_id"] for request in record["requests"][record["next_index"]:]])
            if entry["kind"] == "local":
                if body.get("error"):
                    errors.append(body)
                else:
                    actions.append(body)
            elif entry["kind"] == "harness" and not reply_id:
                if body.get("error"):
                    metadata["error"] = body["error"]
                elif body.get("text"):
                    messages.append(body["text"])
        signatures = {canonical([result.get("request_id"), result.get("data"), result.get("error"), result.get("ok")]) for result in packet["results"]}
        raw_results = [entry["body"] for entry in entries if entry["kind"] == "result"]
        for result in [*raw_results, *aggregate_results]:
            signature = canonical([result.get("request_id"), result.get("data"), result.get("error"), result.get("ok")])
            if result.get("request_id") not in covered and signature not in signatures:
                packet["results"].append(result)
                signatures.add(signature)
        packet.update(metadata)
        if messages:
            packet["messages"] = list(dict.fromkeys(messages))
        if errors:
            packet["local_errors"] = errors
        if actions:
            packet["local_actions"] = actions
        return packet
