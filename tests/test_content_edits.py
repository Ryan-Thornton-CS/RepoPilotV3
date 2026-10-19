"""Full-file proposals share reviewed diff, freshness, encoding, rollback and validation behavior."""

from __future__ import annotations

import json
import sys

import pytest

from conftest import request
from repopilotv3.core import PilotError, atomic_json
from repopilotv3.manual.content_edits import ContentDiff
from repopilotv3.manual.patches import PatchManager, parse_patch, transform
from repopilotv3.manual.schema import parse
from repopilotv3.repository.service import RepositoryTools
from repopilotv3.workflow.service import WorkflowService
from test_workflow import start


def contents_proposal(ws, files):
    """Use real current snapshots and source hashes for synthetic complete-file proposals."""
    snapshot = RepositoryTools(ws).snapshot()
    return {'base_snapshot': snapshot['id'], 'base_hashes': {path: snapshot['hashes'].get(path) for path in files}, 'files': files}


@pytest.mark.parametrize(('original', 'content', 'expected'), [
    (None, '', b''), (b'', None, None), (b'', 'new\n', b'new\n'),
    (None, 'hello', b'hello'), (b'old', 'new', b'new'),
    (b'old\n', 'old', b'old'), (b'old', 'old\n', b'old\n'),
    (b'old\n', '', b''), (b'old', None, None),
    (b'\xef\xbb\xbfold\r\n', 'new\n', b'\xef\xbb\xbfnew\r\n'),
    (b'old\r\n', 'new\r\nnext\r\n', b'new\r\nnext\r\n'),
    (None, 'new\r\nnext\r\n', b'new\nnext\n'),
])
def test_generated_diff_round_trip_handles_counts_and_newlines(original, content, expected):
    """Generate exact counts/markers for additions, deletions, empty files and missing final newlines."""
    diff = ContentDiff.generate('Folder Ω/file.py', original, content)
    assert transform(parse_patch(diff)[0], original) == expected


def test_proposal_schema_accepts_contents_and_legacy_diff_exclusively(ws):
    """Reject ambiguity and malformed content before execution, preserving legacy protocol 1.0."""
    arguments = contents_proposal(ws, {'hello.py': 'print("Hello")\n'})
    assert parse(request('propose_patch', arguments))['arguments'] == arguments
    for change in [{'diff': 'PRIVATE CONTENT'}, {'files': {}}, {'files': {'hello.py': 4}}]:
        with pytest.raises(PilotError) as error:
            parse(request('propose_patch', arguments | change))
        assert error.value.code == 'invalid_request'
        if 'diff' in change:
            assert 'exactly one' in str(error.value) and 'PRIVATE CONTENT' not in str(error.value)
    with pytest.raises(PilotError, match='exactly one'):
        parse(request('propose_patch', {key: value for key, value in arguments.items() if key != 'files'}))


def test_stages_full_contents_as_immutable_diff_skips_unchanged_files_and_applies(ws, session):
    """Review displays generated code and staging cannot write source; only changed files participate."""
    task = start(ws, session)
    original = (ws.root / 'greeter.py').read_text('utf-8')
    value = contents_proposal(ws, {'greeter.py': original, 'Folder Ω/hello.py': '"""Greeting."""\nprint("Hello")\n', 'README.md': None, 'empty.py': ''})
    manager = PatchManager(ws)
    staged = manager.stage(value, session, task)
    assert staged['input_format'] == 'file_contents' and staged['unchanged_files'] == ['greeter.py']
    assert not (ws.root / 'Folder Ω/hello.py').exists() and (ws.root / 'README.md').exists()
    stored = ws.get('patch', staged['patch_id'])
    assert 'files' not in stored['proposal'] and 'diff' in stored['proposal']
    assert 'greeter.py' not in stored['proposal']['base_hashes']
    assert 'Folder Ω/hello.py' in manager.review(staged['patch_id'])['diff']
    manager.apply(staged['patch_id'], True)
    assert (ws.root / 'Folder Ω/hello.py').read_text('utf-8') == value['files']['Folder Ω/hello.py']
    assert (ws.root / 'empty.py').read_bytes() == b'' and not (ws.root / 'README.md').exists()


@pytest.mark.parametrize('path', ['../escape.py', '/escape.py', 'C:/escape.py', '.repopilotv3/config.json', '.git/config', 'folder\nname.py'])
def test_content_paths_are_confined(ws, path):
    """A simpler proposal format never bypasses existing source path restrictions."""
    value = contents_proposal(ws, {path: 'new\n'})
    with pytest.raises(PilotError):
        PatchManager(ws).validate(value)


def test_stale_content_hashes_and_snapshots_are_rejected(ws, session):
    """Changing source before staging or after review never regenerates and silently rebases a proposal."""
    task = start(ws, session)
    manager = PatchManager(ws)
    value = contents_proposal(ws, {'greeter.py': 'new\n'})
    stale = value | {'base_hashes': {'greeter.py': 'wrong'}}
    with pytest.raises(PilotError, match='hash'):
        manager.validate(stale)
    staged = manager.stage(value, session, task)
    manager.review(staged['patch_id'])
    (ws.root / 'greeter.py').write_text('external edit\n', encoding='utf-8')
    for action in [lambda: manager.stage(value, session, task), lambda: manager.apply(staged['patch_id'], True)]:
        with pytest.raises(PilotError) as error:
            action()
        assert error.value.code == 'stale_patch'
    assert (ws.root / 'greeter.py').read_text('utf-8') == 'external edit\n'


def test_content_rejects_invalid_targets_binary_noops_and_missing_hashes(ws):
    """Invalid data produces operational errors without weakening ignored-file or symlink checks."""
    manager = PatchManager(ws)
    for files in [{'missing.py': None}, {'bad.py': '\0'}, {'bad.py': '\ud800'}, {'greeter.py': (ws.root / 'greeter.py').read_text('utf-8')}]:
        with pytest.raises(PilotError):
            manager.validate(contents_proposal(ws, files))
    value = contents_proposal(ws, {'new.py': 'new\n'})
    with pytest.raises(PilotError, match='exactly'):
        manager.validate(value | {'base_hashes': {'other.py': None}})
    config = ws.config
    config['exclude'].append('ignored.py')
    atomic_json(ws.config_path, config)
    with pytest.raises(PilotError):
        manager.validate(contents_proposal(ws, {'ignored.py': 'new\n'}))
    link = ws.root / 'linked.py'
    try:
        link.symlink_to(ws.root / 'greeter.py')
    except OSError:
        return
    with pytest.raises(PilotError):
        manager.validate(contents_proposal(ws, {'linked.py': 'new\n'}))


def test_content_write_failure_uses_existing_rollback(ws, session, monkeypatch):
    """A generated multi-file diff is restored as one proposal when a later write fails."""
    import repopilotv3.manual.patches as module

    task = start(ws, session)
    manager = PatchManager(ws)
    original = (ws.root / 'greeter.py').read_bytes()
    staged = manager.stage(contents_proposal(ws, {'greeter.py': 'new\n', 'new.py': 'new\n'}), session, task)
    manager.review(staged['patch_id'])
    real_write = module.atomic_bytes

    def fail_second(path, data):
        """Inject a source write error after the first target changed."""
        if path == ws.root / 'new.py':
            raise OSError('injected failure')
        return real_write(path, data)

    monkeypatch.setattr(module, 'atomic_bytes', fail_second)
    with pytest.raises(OSError):
        manager.apply(staged['patch_id'], True)
    assert (ws.root / 'greeter.py').read_bytes() == original and not (ws.root / 'new.py').exists()


def test_manual_content_reply_deduplicates_waits_for_approval_and_records_real_tests(ws, session):
    """The full pasted-reply path generates native review data and validates only after local approval."""
    start(ws, session)
    config = ws.config
    config['profiles']['tests'] = {'kind': 'python_test', 'argv': [sys.executable, '-m', 'pytest', '--junitxml={results_dir}/junit.xml'], 'args': [], 'timeout': 60}
    config['default_validation'] = 'tests'
    atomic_json(ws.config_path, config)
    service = WorkflowService(ws)
    source = ws.root / 'greeter.py'
    before = source.read_text('utf-8')
    body = request('propose_patch', contents_proposal(ws, {'greeter.py': before.replace('Hello {name}', 'Hello, {name}!')}), 'contents')
    pending = service.import_reply(session, body)
    assert pending['status'] == 'awaiting_approval' and source.read_text('utf-8') == before
    assert service.import_reply(session, body) == pending
    review = service.review(session, pending['pending']['patch_id'])
    assert review['files'][0]['proposed'].replace('\r\n', '\n').endswith('return f"Hello, {name}!"\n')
    assert not ws.records('validation')
    result = service.resolve_reply(session, pending['reply_id'], True)
    assert result['status'] == 'completed'
    validation = ws.records('validation')[-1]
    assert validation['status'] == 'passed' and validation['evidence_kind'] == 'tests'
    assert sum(int(count['executed']) for count in validation['test_counters']) == 1
    assert json.loads(result['copy_text'])['results']
