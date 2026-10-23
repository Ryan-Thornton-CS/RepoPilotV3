"""Serve a tiny scripted chat on loopback; never call a model or serve repository files."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit


class DemoHandler(BaseHTTPRequestHandler):
    """Allow only bundled assets and suppress chat/browser data in access logs."""

    def do_GET(self) -> None:
        """Return fixed demo resources; traversal and arbitrary filesystem reads fail."""
        route = urlsplit(self.path).path
        assets = {"/": ("index.html", "text/html"), "/demo.js": ("demo.js", "text/javascript"),
                  "/demo.css": ("demo.css", "text/css")}
        if route not in assets:
            self.send_error(404)
            return
        name, content_type = assets[route]
        data = (Path(__file__).parent / "assets" / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        """Keep localhost request details off the JSON-RPC protocol and private logs."""


class DemoServer:
    """Own one explicitly started demo and await complete cleanup on backend shutdown."""

    def __init__(self):
        """Construct without opening a port or starting a background thread."""
        self.server = None
        self.thread = None

    def start(self) -> dict:
        """Bind an ephemeral IPv4-loopback port and reuse a running demo."""
        if self.server is None:
            self.server = ThreadingHTTPServer(("127.0.0.1", 0), DemoHandler)
            self.server.daemon_threads = True
            self.thread = threading.Thread(target=self.server.serve_forever, daemon=True, name="repopilot-demo")
            self.thread.start()
        return self.status()

    def status(self) -> dict:
        """Describe a running server without creating one implicitly."""
        return {"running": self.server is not None,
                "url": f"http://127.0.0.1:{self.server.server_port}/" if self.server else None,
                "scripted": True}

    def stop(self) -> dict:
        """Close the listener and await its thread; normal restarts never replay a demo."""
        if self.server:
            self.server.shutdown()
            self.server.server_close()
            self.thread.join(timeout=5)
            self.server = self.thread = None
        return self.status()
