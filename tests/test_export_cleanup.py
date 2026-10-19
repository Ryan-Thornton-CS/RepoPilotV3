"""Automatic generated-data retention preserves current packages, receipts and recovery evidence."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from repopilotv3.core import DEFAULTS, PilotError, Workspace, atomic_json, digest, load_json
from repopilotv3.exports.cleanup import ExportCleanup
from repopilotv3.exports.manager import ExportManager
from repopilotv3.manual.sessions import SessionStore
from repopilotv3.workflow.service import WorkflowService
from test_repository_exports import bind


def policy(ws, **changes):
    """Set explicit tiny retention thresholds for deterministic synthetic pruning."""
    config = ws.config
    config['export_cleanup'].update(changes)
    atomic_json(ws.config_path, config)


def test_cleanup_default_is_backwards_compatible_without_rewriting_config(ws):
    """Existing configurations load the new defaults in memory without a manual migration."""
    config = ws.config
    config.pop('export_cleanup')
    atomic_json(ws.config_path, config)
    before = ws.config_path.read_bytes()
    assert Workspace(ws.root).config['export_cleanup'] == DEFAULTS['export_cleanup']
    assert ws.config_path.read_bytes() == before
    policy(ws, target_bytes=999, max_bytes=100)
    with pytest.raises(PilotError, match='target_bytes'):
        _ = ws.config


def test_pressure_cleanup_preserves_current_and_previous_generations_and_reuse(ws, session):
    """Reclaim oldest generations/cache while preserving live pointers, current hashes and upload receipts."""
    bind(ws, session)
    policy(ws, enabled=False)
    manager = ExportManager(ws)
    generations = []
    for index in range(5):
        (ws.root / 'notes.txt').write_text(f'Iteration {index}\n' + str(index) * 3000, encoding='utf-8')
        generations.append(manager.prepare(session, True))
    current = generations[-1]
    name = '00_PROJECT_INSTRUCTIONS.md'
    manager.confirm(current['manifest']['id'], session, {name: current['manifest']['artifacts'][name]['hash']})
    outside = ws.state / 'patches/recovery-proof'
    outside.parent.mkdir()
    outside.write_text('Keep recovery', encoding='utf-8')
    orphan_key = digest('unused cache')
    cache = ws.state / 'exports/artifacts'
    (cache / (orphan_key + '.data')).write_bytes(b'x' * 10000)
    atomic_json(cache / (orphan_key + '.json'), {'hash': digest(b'x' * 10000)})
    policy(ws, enabled=True, max_bytes=1, target_bytes=0, interval_seconds=0)
    with ws.lock():
        report = ExportCleanup(ws).run()
    assert report['removed_generations'] == 3 and report['freed_bytes'] > 0
    assert report['over_limit'] and 'protected' in report['warning']
    assert not Path(generations[0]['directory']).exists()
    assert Path(generations[-2]['directory']).exists() and Path(current['directory']).exists()
    assert not (cache / (orphan_key + '.data')).exists()
    assert outside.read_text('utf-8') == 'Keep recovery'
    assert load_json(ws.state / f'exports/current-{session}.json')['id'] == current['manifest']['id']
    assert manager.checklist(current['manifest'])[0]['action'] == 'keep'
    again = manager.prepare(session, True)
    assert again['manifest']['artifacts'][name]['hash'] == current['manifest']['artifacts'][name]['hash']


def test_cleanup_protects_other_chats_and_archived_current_packages(ws, session):
    """Storage pressure never removes another live or archived chat's current upload package."""
    task = bind(ws, session)
    policy(ws, enabled=False)
    manager = ExportManager(ws)
    first = manager.prepare(session)
    other = SessionStore(ws).create(task['id'])['id']
    second = manager.prepare(other)
    SessionStore(ws).update(session, archived=True)
    policy(ws, enabled=True, max_bytes=1, target_bytes=0, keep_generations=1, interval_seconds=0)
    report = ExportCleanup(ws).run()
    assert not report['removed_generations']
    assert Path(first['directory']).exists() and Path(second['directory']).exists()


def test_cached_prepare_runs_cleanup_without_republishing(ws, session):
    """Periodic unchanged-package refresh still reclaims orphan artifacts without forcing re-uploads."""
    service = WorkflowService(ws)
    first = service.prepare(session)
    policy(ws, max_bytes=1, target_bytes=0, keep_generations=1, interval_seconds=0)
    current = service.prepare(session)
    cache = ws.state / 'exports/artifacts'
    orphan = cache / (digest('orphan') + '.data')
    orphan.write_bytes(b'x' * 2000)
    result = service.prepare(session)
    assert result['unchanged'] and result['manifest']['id'] == current['manifest']['id']
    assert result['cleanup']['removed_artifacts'] >= 1 and not orphan.exists()
    assert Path(first['directory']).exists() is False


def test_disabled_and_throttled_cleanup_do_not_rescan(ws, session, monkeypatch):
    """Routine five-second panel refreshes reuse the scan report; disabling retention retains old data."""
    service = WorkflowService(ws)
    service.prepare(session)
    cleanup = ExportCleanup(ws)

    def forbidden(*args):
        """Detect an unnecessary repeated inventory scan."""
        raise AssertionError('Unexpected export scan')

    monkeypatch.setattr(cleanup, 'collect', forbidden)
    assert cleanup.run()['status'] == 'ok'
    policy(ws, enabled=False)
    assert cleanup.run() == {'enabled': False}


def test_linked_storage_and_corrupt_manifests_fail_without_touching_sources(ws, session, tmp_path):
    """A retention pass never follows escaping links or prunes unknown malformed generations."""
    WorkflowService(ws).prepare(session)
    policy(ws, max_bytes=1, target_bytes=0, interval_seconds=0)
    base = ws.state / 'exports'
    malformed = base / 'generation-0123456789abcdef'
    malformed.mkdir()
    (malformed / 'manifest.json').write_text('{broken', encoding='utf-8')
    report = ExportCleanup(ws).run()
    assert report['status'] == 'degraded' and malformed.exists()
    (malformed / 'manifest.json').unlink()
    os.utime(malformed, (time.time() - 7200, time.time() - 7200))
    assert ExportCleanup(ws).run()['removed_generations'] == 1
    external = tmp_path / 'outside'
    external.mkdir()
    linked = base / 'generation-aaaaaaaaaaaaaaaa'
    try:
        linked.symlink_to(external, target_is_directory=True)
    except OSError:
        return
    assert ExportCleanup(ws).run()['status'] == 'degraded'
    assert not list(external.iterdir())


def test_deleted_chat_packages_can_be_reclaimed_without_deleting_task(ws, session):
    """Deleted chats no longer pin old generated source, while shared task and receipts remain."""
    task = bind(ws, session)
    policy(ws, enabled=False)
    manager = ExportManager(ws)
    old = manager.prepare(session)
    other = SessionStore(ws).create(task['id'])['id']
    current = manager.prepare(other)
    WorkflowService(ws).delete(session)
    policy(ws, enabled=True, max_bytes=1, target_bytes=0, keep_generations=1, interval_seconds=0)
    report = ExportCleanup(ws).run()
    assert report['removed_generations'] == 1
    assert not Path(old['directory']).exists() and Path(current['directory']).exists()
    assert ws.get('task', task['id'])['objective'] == task['objective']


def test_interrupted_cache_sweep_removes_metadata_before_data(ws, session, monkeypatch):
    """A stopped prune leaves a cache miss rather than a broken metadata hit on next publication."""
    WorkflowService(ws).prepare(session)
    policy(ws, max_bytes=1, target_bytes=0, interval_seconds=0)
    cache = ws.state / 'exports/artifacts'
    data = cache / (digest('interrupted orphan') + '.data')
    metadata = data.with_suffix('.json')
    data.write_bytes(b'orphan')
    atomic_json(metadata, {'hash': digest(b'orphan')})
    real_unlink = Path.unlink

    def interrupt(path, *args, **kwargs):
        """Fail only at the orphan data boundary after its metadata should be gone."""
        if path == data:
            raise OSError('interrupted cache deletion')
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, 'unlink', interrupt)
        assert ExportCleanup(ws).run()['status'] == 'degraded'
    assert data.exists() and not metadata.exists()
    assert ExportCleanup(ws).run()['removed_artifacts'] >= 1 and not data.exists()
