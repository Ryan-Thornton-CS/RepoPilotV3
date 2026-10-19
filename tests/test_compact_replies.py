"""Exercise compact model-facing output and unambiguous repository discovery."""

import json

import pytest

from conftest import request
from repopilotv3.core import PilotError, Workspace, canonical
from repopilotv3.repository.service import RepositoryTools
from repopilotv3.workflow.formatting import ReplyFormatter
from repopilotv3.workflow.replies import parse_reply
from repopilotv3.workflow.service import WorkflowService
from repopilotv3.workflow.turns import ConversationTurns


def test_task_and_todo_are_copied_once_without_losing_evidence(ws, session):
    """Latest progress replaces duplicate presentations while local absence of tests stays explicit."""
    service = WorkflowService(ws)
    imported = service.import_reply(session, request('task', {'objective': 'Add a greeting'}, 'task') + '\n' + request('todo', {'progress': {'items': [{'id': 'inspect', 'text': 'Inspect source', 'status': 'active'}]}}, 'todo'))
    copied = ConversationTurns(service.history).copy(session, 'reply-result:' + imported['reply_id'])
    data = copied['copy_data']
    assert data['task']['items'][0]['verification'] == 'unverified'
    assert data['task']['validation'] == []
    assert len(data['results']) == 2
    assert data['results'][1]['data']['default_validation'] is None
    assert data['results'][1]['data']['validation_profiles'] == []
    assert 'created_at' not in copied['copy_text'] and 'monitoring' not in copied['copy_text']
    assert '"todo":' not in copied['copy_text']
    assert len(copied['copy_text']) < len(canonical(imported['results'])) * .7
    assert json.loads(copied['copy_text'].split('```json\n')[1].split('\n```')[0]) == data
    assert data == ReplyFormatter().compact(data)


def test_compaction_preserves_sources_errors_warnings_and_distinct_calls():
    """Source and failure evidence survive compaction without hiding incomplete monitoring."""
    value = {'session_id': 'chat', 'status': 'failed', 'remaining_request_ids': ['later'], 'results': [
        {'request_id': 'read', 'ok': True, 'data': {'path': 'Ω.py', 'start': 1, 'end': 2, 'hash': 'abc', 'snapshot': 'snap', 'text': 'a\r\nb\r\n', 'omissions': [{'path': 'large.py'}]}, 'monitoring': {'warnings': ['Repeat warning'], 'coverage': 'partial'}},
        {'request_id': 'test', 'ok': False, 'error': {'code': 'validation_failed', 'message': 'Actual failure'}, 'data': {'passed': False, 'executed_tests': 0}}
    ]}
    result = ReplyFormatter().compact(value)
    assert result['results'][0]['data'] == value['results'][0]['data']
    assert result['results'][1] == value['results'][1]
    assert result['remaining_request_ids'] == ['later']
    assert result['warnings'] == ['Repeat warning', 'Monitoring coverage: partial']
    assert 'Correct' in result['next']
    assert 'monitoring' in value['results'][0]


@pytest.mark.parametrize('legacy', [False, True])
@pytest.mark.parametrize('fenced', [False, True])
def test_pasted_results_are_not_executed_or_echoed(ws, session, legacy, fenced):
    """Both old event envelopes and compact results return concise corrective errors before effects."""
    data = {'protocol_version': '1.0', 'session_id': session}
    data.update({'events': [{'body': {'text': 'PRIVATE' * 1000}}]} if legacy else {'kind': 'tool_results', 'ok': True, 'results': []})
    text = canonical(data)
    if fenced:
        text = 'Tool output\n```json\n' + text + '\n```'
    with pytest.raises(PilotError) as error:
        parse_reply(text)
    assert error.value.code == 'response_pasted'
    assert len(str(error.value)) < 300 and 'PRIVATE' not in str(error.value)
    assert WorkflowService(ws).status(session)['task'] is None


def test_file_discovery_distinguishes_no_matches_from_empty_index(ws, tmp_path):
    """Explicit bounded path discovery and file counts remove ambiguity for keyword-only search."""
    repo = RepositoryTools(ws)
    unmatched = repo.search('no_such_identifier_123456')
    assert unmatched['indexed_file_count'] > 0 and not unmatched['hits']
    listing = repo.search('*', 1)
    assert listing['search_kind'] == 'file_list' and len(listing['hits']) == 1
    assert listing['remaining_files'] == listing['indexed_file_count'] - 1
    assert listing['hits'][0]['hash']
    assert not listing['hits'][0]['path'].startswith('.repopilot')
    root = tmp_path / 'Empty Ω'
    root.mkdir()
    empty = Workspace(root)
    empty.initialize()
    result = RepositoryTools(empty).search('*')
    assert result['indexed_file_count'] == 0 and 'null base hashes' in result['next']


def test_same_data_from_distinct_requests_is_not_dropped(ws, session):
    """Copy preserves independent request identities even when their returned data matches."""
    history = WorkflowService(ws).history
    for name in ['a', 'b']:
        history.add(session, 'result', {'request_id': name, 'ok': True, 'data': {'value': 1}}, name)
    copied = ConversationTurns(history).copy(session, 'b')
    assert [result['request_id'] for result in copied['copy_data']['results']] == ['a', 'b']


def test_preview_failure_records_no_uncertain_execution(ws, session):
    """Rejecting a result during read-only preflight never suggests that source effects occurred."""
    import io

    from repopilotv3.rpc.server import BridgeServer

    server = BridgeServer(ws, io.StringIO())
    try:
        server.respond({'id': 1}, 'replies.preview', {'session_id': session, 'text': canonical({'kind': 'tool_results', 'protocol_version': '1.0', 'results': []})})
        failure = server.service.history.page(session)[-1]['body']
        assert failure['error']['code'] == 'response_pasted'
        assert failure['effects_may_be_uncertain'] is False
    finally:
        server.executor.shutdown()
        server.admin.shutdown()


def test_local_failure_overrides_an_older_success_instruction():
    """A later local error is visible even when the preceding tools completed successfully."""
    text, data = ReplyFormatter().render({'status': 'completed', 'next': 'Continue normally', 'results': [{'request_id': 'done', 'ok': True, 'data': {}}], 'local_errors': [{'error': {'code': 'response_pasted', 'message': 'Return a request'}}]})
    assert text.startswith('RepoPilot: action needs attention.')
    assert data['next'].startswith('Correct the reported error')
