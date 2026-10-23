"""Synthetic state upgrades, browser bookkeeping and loopback demo regression tests."""

from __future__ import annotations

import io
import shutil
import sqlite3
import urllib.error
import urllib.request

import pytest

from repopilotv3.browser.demo import DemoServer
from repopilotv3.browser.settings import BrowserSettings
from repopilotv3.core import PilotError, Workspace, atomic_json
from repopilotv3.rpc.server import BridgeServer
from repopilotv3.storage.migration import StateMigration
from repopilotv3.workflow.service import WorkflowService


def legacy_workspace(ws):
    """Create the previous on-disk layout from closed synthetic state only."""
    service = WorkflowService(ws)
    status = service.create()
    service.history.preference("sentinel", {"draft": "Keep Ω draft"})
    config = ws.config
    for path in (ws.state / "databases").glob("*"):
        shutil.move(path, ws.state / path.name)
    config["schema_version"] = "1.0"
    atomic_json(ws.config_path, config)
    return Workspace(ws.root), status["session"]["id"]


def test_new_state_groups_databases_locks_and_runtime(ws, session):
    """New state creates no loose database/operation files while preserving session data."""
    WorkflowService(ws).status(session)
    with ws.lock(), ws.lock(scope="session:" + session):
        assert (ws.state / "locks/writer.lock").exists()
    assert list((ws.state / "databases").glob("*.sqlite3"))
    assert not list(ws.state.glob("*.sqlite3"))
    assert not list(ws.state.glob("*.lock"))
    assert ws.config["schema_version"] == "2.0"


def test_legacy_migration_preserves_history_and_is_idempotent(ws):
    """An explicit offline migration retains records and verified database backups."""
    legacy, session = legacy_workspace(ws)
    assert legacy.paths.legacy
    with legacy.lock(scope="session:" + session):
        pass
    migration = StateMigration(legacy)
    with pytest.raises(PilotError, match="Confirm"):
        migration.migrate()
    result = migration.migrate(True)
    assert result["layout"] == "organized"
    assert result["journal"]["status"] == "complete"
    restored = WorkflowService(Workspace(ws.root))
    assert restored.sessions.get(session)["id"] == session
    assert restored.history.preference("sentinel")["draft"] == "Keep Ω draft"
    assert not list(ws.state.glob("operation-*.lock"))
    assert not list(ws.state.glob("*.sqlite3*"))
    assert migration.migrate(True)["layout"] == "organized"
    backups = list((ws.state / "recovery/layout").glob("migration-*/*.sqlite3"))
    assert backups
    with sqlite3.connect(backups[0]) as db:
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


def test_busy_state_is_not_migrated(ws):
    """An active legacy writer cannot be bypassed by the new lock layout."""
    legacy, _ = legacy_workspace(ws)
    with legacy.lock():
        with pytest.raises(PilotError) as error:
            StateMigration(legacy).migrate(True)
    assert error.value.code == "busy"
    assert legacy.paths.legacy


def test_migration_closes_sqlite_handles_before_removing_old_paths(ws, monkeypatch):
    """SQLite context managers commit but do not close; Windows needs explicit closure."""
    legacy, _ = legacy_workspace(ws)
    connect = sqlite3.connect
    connections = []

    def track(*args, **kwargs):
        """Keep strong references so garbage collection cannot disguise leaked handles."""
        connection = connect(*args, **kwargs)
        connections.append(connection)
        return connection

    monkeypatch.setattr(sqlite3, "connect", track)
    StateMigration(legacy).migrate(True)
    assert connections
    for connection in connections:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")


def test_failed_migration_rolls_back_configuration_and_keeps_originals(ws, monkeypatch):
    """A failed verified copy leaves the legacy session fully readable."""
    legacy, session = legacy_workspace(ws)
    original = shutil.copy2

    def fail_copy(source, destination, *args, **kwargs):
        """Inject a filesystem failure after database copying starts."""
        if str(destination).endswith("index.sqlite3"):
            raise OSError("synthetic disk failure")
        return original(source, destination, *args, **kwargs)

    monkeypatch.setattr(shutil, "copy2", fail_copy)
    with pytest.raises(OSError, match="synthetic"):
        StateMigration(legacy).migrate(True)
    assert StateMigration(legacy).status()["journal"]["status"] == "rolled_back"
    assert WorkflowService(legacy).sessions.get(session)["id"] == session


def test_interrupted_migration_can_be_recovered_before_startup(ws):
    """Incomplete publication blocks services but leaves hello/recovery diagnostics available."""
    legacy, session = legacy_workspace(ws)
    recovery = legacy.state / "recovery/layout/migration-test"
    recovery.mkdir(parents=True)
    shutil.copy2(legacy.config_path, recovery / "config.json")
    atomic_json(legacy.state / "recovery/layout/current.json", {"id": "migration-test", "status": "copying"})
    with pytest.raises(PilotError) as error:
        WorkflowService(legacy)
    assert error.value.code == "migration_required"
    server = BridgeServer(legacy, io.StringIO())
    try:
        assert not server.call("hello", {})["initialized"]
        assert server.call("state.status", {})["recovery_required"]
        assert StateMigration(legacy).migrate(True)["journal"]["status"] == "rolled_back"
        assert WorkflowService(legacy).sessions.get(session)["id"] == session
    finally:
        server.executor.shutdown()
        server.admin.shutdown()


def test_browser_settings_are_private_scoped_and_never_probe(ws, monkeypatch):
    """Store arbitrary private HTTP hosts without contacting them or arming a session."""
    service = WorkflowService(ws)
    a, b = service.create()["session"]["id"], service.create()["session"]["id"]
    browser = BrowserSettings(service)

    def unexpected(*args, **kwargs):
        """Any attempted availability request is a regression."""
        pytest.fail("Saving a URL must not contact the site")

    monkeypatch.setattr(urllib.request, "urlopen", unexpected)
    browser.configure(settings={"url": "https://private-chat.internal:8443/project"})
    browser.configure(a, {"url": "http://127.0.0.1:4321/chat/one", "mode": "loop", "enabled": True})
    assert browser.configure(a)["settings"]["mode"] == "loop"
    assert not browser.configure(a)["armed"]
    assert browser.configure(b)["settings"]["url"].startswith("https://private-chat.internal")
    browser.configure(a, {"url": ""})
    assert browser.configure(a)["settings"]["url"].startswith("https://private-chat.internal")
    for url in ("javascript:alert(1)", "file:///etc/passwd", "https://user:password@host/", "http://bad host"):
        with pytest.raises(PilotError):
            browser.configure(a, {"url": url})


def test_browser_journals_do_not_count_fills_or_duplicate_sends(ws, session):
    """Only confirmed sends count, sharing the existing explicit-copy event identity."""
    service = WorkflowService(ws)
    browser = BrowserSettings(service)
    browser.transfer(session, "response-one", "fill_pending", "exact response")
    browser.transfer(session, "response-one", "filled", "exact response")
    assert service.sessions.metrics(session)["events"] == 0
    service.transfer(session, "exact response")
    browser.transfer(session, "response-one", "send_pending", "exact response")
    browser.transfer(session, "response-one", "sent", "exact response")
    browser.transfer(session, "response-one", "sent", "exact response")
    assert service.sessions.metrics(session)["events"] == 1
    with pytest.raises(PilotError) as error:
        browser.transfer(session, "response-one", "sent", "different")
    assert error.value.code == "request_conflict"


def test_site_mappings_reuse_without_arming_or_changing_modes(ws):
    """A new chat can reuse a site's selectors while permissions and running state stay separate."""
    service = WorkflowService(ws)
    a, b = service.create()["session"]["id"], service.create()["session"]["id"]
    browser = BrowserSettings(service)
    selectors = {"input": "textarea", "send": "#send"}
    browser.configure(a, {"url": "https://private/chat/one", "selectors": selectors, "enabled": True, "mode": "loop"})
    inherited = browser.configure(b, {"url": "https://private/chat/two"})
    assert inherited["settings"]["selectors"] == selectors
    assert not inherited["settings"]["enabled"] and not inherited["armed"]
    assert inherited["settings"]["mode"] == "fill"
    changed = browser.configure(b, {"url": "https://another-private/chat"})
    assert changed["settings"]["selectors"] == {}


def test_demo_serves_only_local_bundled_assets_and_shuts_down():
    """The minimal mock has explicit completion, no external requests and no file serving."""
    server = DemoServer()
    assert not server.status()["running"]
    try:
        result = server.start()
        assert result == server.start()
        assert server.server.server_address[0] == "127.0.0.1"
        with urllib.request.urlopen(result["url"], timeout=3) as response:
            html = response.read().decode()
            assert "Scripted demo" in html and "chat-input" in html
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        with urllib.request.urlopen(result["url"] + "demo.js", timeout=3) as response:
            script = response.read().decode()
            assert "fetch(" not in script and "innerHTML" not in script
            assert "model.dataset.complete='true'" in script
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(result["url"] + "../../config.json", timeout=3)
    finally:
        assert not server.stop()["running"]
        assert not server.stop()["running"]


def test_browser_management_schema_and_shutdown_are_closed(ws):
    """The server owns the demo and rejects arbitrary scripts in browser preferences."""
    server = BridgeServer(ws, io.StringIO())
    request = {"jsonrpc": "2.0", "id": 1, "method": "browser.settings",
               "params": {"workspace": str(ws.root), "bridge_version": "1.0", "settings": {"script": "bad"}}}
    try:
        with pytest.raises(PilotError):
            server.validate(request)
        server.call("browser.demo.start", {})
        server.serve(io.StringIO(""))
        assert not server.demo.status()["running"]
    finally:
        server.demo.stop()
        server.executor.shutdown()
        server.admin.shutdown()
