"""Standalone repository MCP bootstrap and explicitly approved workspace harness generation."""

from __future__ import annotations

import asyncio
import io

import pytest
from fastmcp import Client

from repopilotv3.core import PilotError, Workspace
from repopilotv3.integrations.harness import HarnessManager, BEGIN, END
from repopilotv3.integrations.mcp import create_server
from repopilotv3.rpc.server import BridgeServer


def test_uninitialized_mcp_discovers_initializes_and_binds_projects_independently(tmp_path):
    """Real MCP setup needs no prior CLI/TUI and cannot change another call's default root."""
    async def scenario():
        """Exercise the asynchronous MCP client without a pytest plugin dependency."""
        root = tmp_path / 'Multi Ω projects'
        root.mkdir()
        (root / 'root.py').write_text('"""Parent repository."""\n', encoding='utf-8')
        for name in ['A', 'B']:
            (root / name).mkdir()
            (root / name / 'source.py').write_text(f'"""Repository {name}."""\n', encoding='utf-8')
        workspace = Workspace(root)
        async with Client(create_server(workspace)) as client:
            tools = {tool.name: tool for tool in await client.list_tools()}
            assert len(tools) == 6 and not workspace.state.exists()
            assert tools['read_source'].annotations.readOnlyHint is True
            assert tools['setup_repository'].annotations.readOnlyHint is False
            assert not (await client.call_tool('repository_status')).data['initialized']
            with pytest.raises(Exception, match='setup_repository'):
                await client.call_tool('search_repository', {'query': '*'})
            first = (await client.call_tool('setup_repository', {'repository_path': 'A'})).data
            second = (await client.call_tool('setup_repository', {'repository_path': 'B'})).data
            assert first['repository_id'] != second['repository_id']
            assert not first['harness_written'] and not first['dependencies_installed']
            assert not workspace.config_path.exists()
            for result, expected in [(first, 'Repository A'), (second, 'Repository B')]:
                source = await client.call_tool('read_source', {'path': 'source.py', 'repository_id': result['repository_id']})
                assert expected in source.data['data']['text']
            assert not (await client.call_tool('repository_status')).data['initialized']
            await client.call_tool('setup_repository')
            assert (await client.call_tool('read_source', {'path': 'root.py'})).data['data']['path'] == 'root.py'
            assert not (root / '.github').exists()
            with pytest.raises(Exception, match='Unknown repository_id'):
                await client.call_tool('repository_status', {'repository_id': 'repo-unknown'})

    asyncio.run(scenario())


def test_mcp_setup_rejects_escaping_private_and_symlinked_roots(tmp_path):
    """A model cannot use repository setup to widen its locally configured source boundary."""
    async def scenario():
        """Exercise the asynchronous MCP client without a pytest plugin dependency."""
        root, outside = tmp_path / 'Root', tmp_path / 'Other'
        root.mkdir()
        outside.mkdir()
        (root / '.repopilotv3').mkdir()
        async with Client(create_server(Workspace(root))) as client:
            for target in ['../Other', str(outside), '.repopilotv3', 'missing']:
                with pytest.raises(Exception, match='inside|Private'):
                    await client.call_tool('setup_repository', {'repository_path': target})
            link = root / 'external'
            try:
                link.symlink_to(outside, target_is_directory=True)
            except OSError:
                return
            with pytest.raises(Exception, match='inside'):
                await client.call_tool('setup_repository', {'repository_path': 'external'})
        assert not (outside / '.repopilotv3').exists()

    asyncio.run(scenario())


def test_harness_preview_apply_idempotence_and_unrelated_instructions(ws):
    """Explicit generation preserves unrelated instructions and creates no MCP config by default."""
    instructions = ws.root / '.github/copilot-instructions.md'
    instructions.parent.mkdir()
    instructions.write_bytes(b'# User guidance\r\n\r\nKeep my rules.\r\n')
    manager = HarnessManager(ws)
    preview = manager.preview(server_name='repopilotv3-test')
    assert not preview['conflicts'] and not (ws.root / '.github/agents').exists()
    assert 'Keep my rules.' in next(row['proposed'] for row in preview['files'] if row['path'].endswith('copilot-instructions.md'))
    result = manager.apply(preview['plan_id'])
    assert len(result['changed_files']) == 6
    assert not (ws.root / '.mcp.json').exists() and not (ws.root / '.vscode/mcp.json').exists()
    text = instructions.read_bytes().decode('utf-8')
    assert text.startswith('# User guidance\r\n') and text.count(BEGIN) == text.count(END) == 1
    assert 'repopilotv3-test/*' in (ws.root / '.github/agents/repopilotv3-code.agent.md').read_text('utf-8')
    assert not manager.status()['stale_runtime']
    instructions.write_bytes(instructions.read_bytes() + b'\r\nExtra user guidance.\r\n')
    again = manager.preview(server_name='repopilotv3-test')
    assert not again['conflicts'] and all(row['action'] == 'keep' for row in again['files'])
    assert manager.apply(again['plan_id'])['changed_files'] == []


def test_harness_conflicts_stale_preview_and_symlinks_preserve_local_files(ws):
    """Generation refuses modified/unowned agents and changes made after the developer preview."""
    manager = HarnessManager(ws)
    first = manager.preview()
    target = ws.root / first['files'][0]['path']
    target.parent.mkdir(parents=True)
    target.write_text('Local agent', encoding='utf-8')
    with pytest.raises(PilotError, match='after preview'):
        manager.apply(first['plan_id'])
    conflict = manager.preview()
    assert first['files'][0]['path'] in conflict['conflicts']
    with pytest.raises(PilotError, match='conflicts'):
        manager.apply(conflict['plan_id'])
    assert target.read_text('utf-8') == 'Local agent'
    target.unlink()
    try:
        target.symlink_to(ws.root / 'greeter.py')
    except OSError:
        return
    with pytest.raises(PilotError, match='Symlink'):
        manager.preview()


def test_harness_updates_managed_files_only_and_quotes_commands(ws, monkeypatch):
    """An upgrade yields inspectable edits and quotes host paths without embedding event payloads."""
    import json
    import repopilotv3.integrations.harness as module

    manager = HarnessManager(ws)
    manager.apply(manager.preview()['plan_id'])
    original = manager.source

    def changed(name):
        """Model a new static bundle without changing user-owned workspace text."""
        return original(name) + ('\nNew role guidance.\n' if name.endswith('code.agent.md') else '')

    monkeypatch.setattr(manager, 'source', changed)
    monkeypatch.setattr(module.sys, 'executable', "C:/User Tools/Ryan's Python/python.exe")
    assert manager.status()['stale_runtime']
    preview = manager.preview()
    assert not preview['conflicts']
    hooks = json.loads(next(row['proposed'] for row in preview['files'] if row['path'].endswith('repopilotv3.json')))
    command = hooks['hooks']['postToolUse'][0]
    assert "Ryan''s Python" in command['powershell'] and '-I' in command['bash']
    assert 'tool_input' not in command['powershell']
    assert any(row['action'] == 'update' for row in preview['files'])


def test_harness_failure_rolls_back_and_recovery_preserves_later_edits(ws, monkeypatch):
    """A failed second write restores prior source while interrupted recovery refuses newer changes."""
    import repopilotv3.integrations.harness as module

    manager = HarnessManager(ws)
    preview = manager.preview()
    real = module.atomic_bytes
    fail_path = ws.root / preview['files'][1]['path']

    def fail(path, content):
        """Fail one generated file while allowing backup and rollback writes."""
        if path == fail_path:
            raise OSError('injected generation failure')
        return real(path, content)

    with monkeypatch.context() as patch:
        patch.setattr(module, 'atomic_bytes', fail)
        with pytest.raises(OSError):
            manager.apply(preview['plan_id'])
    assert not (ws.root / preview['files'][0]['path']).exists()
    assert not manager.status()['recovery_required']
    fresh = manager.preview()
    manager.apply(fresh['plan_id'])
    recovery = manager.base / 'recovery' / fresh['plan_id'] / 'journal.json'
    from repopilotv3.core import atomic_json, load_json

    record = load_json(recovery)
    record['status'] = 'prepared'
    atomic_json(recovery, record)
    user_file = ws.root / fresh['files'][0]['path']
    user_file.write_text('Later developer change', encoding='utf-8')
    with pytest.raises(PilotError, match='later edit'):
        manager.recover(fresh['plan_id'])
    assert user_file.read_text('utf-8') == 'Later developer change'


def test_management_harness_actions_do_not_need_manual_sessions(ws):
    """Trusted local RPC exposes generation separately from model-callable manual operations."""
    server = BridgeServer(ws, io.StringIO())
    try:
        preview = server.call('harness.preview', {'server_name': 'repopilotv3-example'})
        server.call('harness.apply', {'plan_id': preview['plan_id']})
        assert server.call('harness.status', {})['generated']
        assert not ws.records('session')
    finally:
        server.executor.shutdown()
        server.admin.shutdown()


def test_harness_status_does_not_wait_for_repository_writer(ws):
    """Cheap integration status stays responsive while repository indexing or commands own the writer."""
    server = BridgeServer(ws, io.StringIO())
    try:
        with ws.lock():
            assert not server.call('harness.status', {})['generated']
    finally:
        server.executor.shutdown()
        server.admin.shutdown()
