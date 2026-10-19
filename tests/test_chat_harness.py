"""Synthetic chat workflow, local authorization, command supervision and context accounting."""

from __future__ import annotations

import difflib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from conftest import request
from repopilotv3.core import PilotError
from repopilotv3.manual.commands import CommandRunner
from repopilotv3.manual.sessions import SessionStore
from repopilotv3.repository.service import RepositoryTools
from repopilotv3.tasks.store import TaskStore
from repopilotv3.workflow.replies import parse_reply
from repopilotv3.workflow.service import WorkflowService


def fenced(tool, arguments, key):
    """Wrap one explicitly requested JSON tool operation in a complete Markdown fence."""
    return '```json\n' + request(tool, arguments, key) + '\n```'


def ready(ws, session):
    """Initialize one task and checklist through a single complete pasted model reply."""
    service = WorkflowService(ws)
    text = 'I will plan this.\n' + fenced('task', {'objective': 'Fix greeting', 'chat_name': 'Greeting fix'}, 'task')
    text += '\n' + fenced('todo', {'progress': {'items': [{'id': 'fix', 'text': 'Fix punctuation', 'status': 'active'}]}}, 'todo')
    assert service.import_reply(session, text)['status'] == 'completed'
    return service


def test_one_click_session_starter_and_passive_restoration(ws):
    """A blank chat gets an idempotent introduction separate from reusable project instructions."""
    service = WorkflowService(ws)
    created = service.create()
    session = created['session']['id']
    assert created['task'] is None and created['session']['name'] == 'New session'
    assert created['session']['mode'] == 'plan' and not created['session']['commands_allowed']
    starter = service.starter(session)
    assert session in starter['text'] and 'chat_name' in starter['text']
    assert starter['starter_version'] == 2 and starter['kind'] == 'new'
    assert '```' not in starter['text'] and 'Repository orientation' not in starter['text']
    assert service.starter(session) == starter
    count = len(service.history.page(session))
    WorkflowService(ws).status(session)
    assert len(service.history.page(session)) == count
    assert service.sessions.metrics(session)['events'] == 0


def test_model_tasks_todo_revisions_and_reply_deduplication(ws, session):
    """One pasted reply is one event; repeats cannot duplicate tools, requirements or chat creation."""
    service = ready(ws, session)
    saved = service.status(session)
    task_id = saved['task']['id']
    assert saved['session']['name'] == 'Greeting fix'
    assert saved['metrics']['events'] == 1
    update = fenced('task', {'objective': 'Fix punctuation without API changes', 'chat_name': 'Punctuation'}, 'refine')
    first = service.import_reply(session, update)
    count = len(service.history.page(session))
    assert service.import_reply(session, update) == first
    assert len(service.history.page(session)) == count
    assert service.status(session)['session']['task_id'] == task_id
    task = TaskStore(ws).get(task_id)
    assert task['history'][0]['objective'] == 'Fix greeting' and len(task['items']) == 1
    todo = request('todo', {'progress': {'items': [{'id': 'fix', 'text': 'Fix and verify punctuation', 'status': 'completed'}]}}, 'reword')
    service.import_reply(session, todo)
    task = TaskStore(ws).get(task_id)
    assert task['items'][0]['verification'] == 'unverified' and task['validation'] == []
    assert any('item' in change for change in task['history'])


def test_initial_todo_gate_and_no_model_permission_api(ws, session):
    """Naming/creating a task does not authorize repository use or administrative execution settings."""
    service = WorkflowService(ws)
    service.import_reply(session, request('task', {'objective': 'Read source'}, 't'))
    response = service.import_reply(session, request('read', {'path': 'greeter.py'}, 'r'))
    assert response['results'][0]['error']['code'] == 'todo_required'
    invalid = service.import_reply(session, request('sessions.configure', {'commands_allowed': True}, 'bad'))
    assert not invalid['ok'] and not service.status(session)['session']['commands_allowed']


@pytest.mark.parametrize('broken', [
    '```json\n{"protocol_version":"1.0","tool":"read",}\n```',
    '```json\n{"tool":"read"}',
    fenced('read', {}, 'missing-path'),
    fenced('unknown', {}, 'unknown'),
])
def test_whole_reply_validation_precedes_every_effect(ws, session, broken):
    """One malformed later block prevents an earlier task mutation and reports a copyable cause."""
    service = WorkflowService(ws)
    text = fenced('task', {'objective': 'Must not exist'}, 'first') + '\n' + broken
    result = service.import_reply(session, text)
    assert not result['ok'] and service.status(session)['task'] is None
    assert result['copy_text'] and 'No requests' in result['next']


def test_prose_and_unrelated_json_are_data_and_read_batch_stays_bounded(ws, session):
    """Examples without a protocol envelope never run; repeated batch operations count as one reply."""
    service = ready(ws, session)
    before = service.sessions.metrics(session)['events']
    text = 'Here is data.\n```json\n{"example":"dotnet build"}\n```'
    assert service.import_reply(session, text)['status'] == 'completed'
    batch = request('batch', {'calls': [{'tool': 'read', 'arguments': {'path': 'greeter.py'}}, {'tool': 'search', 'arguments': {'query': 'Greeter'}}]}, 'batch')
    service.import_reply(session, batch)
    assert service.sessions.metrics(session)['events'] == before + 2
    with pytest.raises(PilotError):
        parse_reply('\n'.join(fenced('read', {'path': 'greeter.py'}, str(i)) for i in range(9)))


def test_command_permission_resolution_and_no_replay(ws, session):
    """The local grant executes the staged command once and duplicate approvals cannot run it again."""
    service = ready(ws, session)
    text = request('run_command', {'argv': ['python', '-c', "from pathlib import Path; Path('count.txt').write_text('once'); print('actual output')"]}, 'cmd')
    pending = service.import_reply(session, text)
    assert pending['pending']['kind'] == 'command' and not pending['copy_text']
    assert not (ws.root / 'count.txt').exists()
    assert service.status(session)['pending_replies'][0]['reply_id'] == pending['reply_id']
    completed = service.resolve_reply(session, pending['reply_id'], True)
    record = completed['results'][0]['data']
    assert record['status'] == 'completed' and record['exit_code'] == 0
    assert record['argv'][0] == sys.executable and 'actual output' in record['output']
    assert 'Fatal Python' not in record['output']
    command = record['command_id']
    assert CommandRunner(ws).output(session, command)['text'] == 'actual output\n'
    assert CommandRunner(ws).diffs(session, command, 'count.txt')['files'][0]['proposed'] == 'once'
    assert record['evidence_kind'] == 'command'
    assert service.import_reply(session, text)['status'] == 'completed'
    with pytest.raises(PilotError):
        service.resolve_reply(session, pending['reply_id'], True)
    assert WorkflowService(ws).status(session)['session']['commands_allowed']
    other = service.create()['session']['id']
    with pytest.raises(PilotError):
        CommandRunner(ws).output(other, command)
    service.configure(session, commands_allowed=False)
    assert not service.sessions.get(session)['commands_allowed']


def test_denied_command_stops_unexecuted_requests(ws, session):
    """Declining a command does not execute either it or a later queued operation."""
    service = ready(ws, session)
    text = fenced('run_command', {'argv': ['python', '-c', "print('no')"]}, 'cmd')
    text += '\n' + fenced('task', {'objective': 'Must not change'}, 'later')
    pending = service.import_reply(session, text)
    rejected = service.resolve_reply(session, pending['reply_id'], False)
    assert rejected['remaining_request_ids'] == ['later']
    assert service.status(session)['task']['objective'] == 'Fix greeting'
    assert not service.sessions.get(session)['commands_allowed']


def test_command_paths_missing_executable_and_failure_stop(ws, session):
    """Confinement, executable diagnostics and nonzero exit status produce genuine actionable results."""
    service = ready(ws, session)
    service.configure(session, commands_allowed=True)
    wrong = service.import_reply(session, request('run_command', {'argv': ['not-a-real-executable-xyz']}, 'missing'))
    assert wrong['results'][0]['error']['code'] == 'missing_executable'
    escaped = service.import_reply(session, request('run_command', {'argv': ['python', '-c', 'print(1)'], 'cwd': '../'}, 'escape'))
    assert escaped['results'][0]['error']['code'] == 'unsafe_path'
    failed = service.import_reply(session, request('run_command', {'argv': ['python', '-c', 'raise SystemExit(7)']}, 'failed'))
    assert failed['status'] == 'failed' and failed['results'][0]['data']['exit_code'] == 7


def test_live_command_cancellation_and_responsive_status(ws, session):
    """Cancellation bypasses the repository writer lock and kills child descendants."""
    service = ready(ws, session)
    service.configure(session, commands_allowed=True)
    started = threading.Event()
    command_ids = []

    def notify(value):
        """Capture the running command ID without blocking the worker's stdout drain."""
        command_ids.append(value['command_id'])
        started.set()

    service.notify = notify
    script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',\"import time; from pathlib import Path; time.sleep(2); Path('escaped.txt').write_text('bad')\"]); print('started',flush=True); time.sleep(30)"
    with ThreadPoolExecutor() as executor:
        future = executor.submit(service.import_reply, session, request('run_command', {'argv': ['python', '-c', script]}, 'cancel'))
        assert started.wait(5)
        assert WorkflowService(ws).status(session)['session']['id'] == session
        result = CommandRunner(ws).cancel(session, command_ids[0])
        assert result['cancellation_requested']
        completed = future.result(timeout=10)
    assert completed['results'][0]['data']['status'] == 'cancelled'
    time.sleep(2.1)
    assert not (ws.root / 'escaped.txt').exists()


def test_command_timeout_and_bounded_log_inspection(ws, session):
    """Timeout is separate from exit status and large logs expand through bounded reads."""
    service = ready(ws, session)
    service.configure(session, commands_allowed=True)
    result = service.import_reply(session, request('run_command', {'argv': ['python', '-c', "import time; print('line\\n'*900,flush=True); time.sleep(10)"], 'timeout': 1}, 'timeout'))
    record = result['results'][0]['data']
    assert record['status'] == 'timed_out'
    output = CommandRunner(ws).output(session, record['command_id'], 1, 900)
    assert output['end'] <= 500 and output['omitted']


def test_mode_compose_context_advice_and_continuation(ws, session):
    """Modes are advisory, typed text never executes, and new-chat handoffs reset permission only."""
    service = ready(ws, session)
    service.configure(session, mode='code', commands_allowed=True, context_limit=1000)
    text = 'Run this as discussion only: ' + request('run_command', {'argv': ['missing']}, 'discussion')
    prompt = service.compose(session, text)
    assert text in prompt['copy_text'] and 'Implementation' in prompt['copy_text']
    assert not ws.records('command')
    service.transfer(session, 'x' * 4000)
    result = service.import_reply(session, request('read', {'path': 'greeter.py'}, 'first'))
    assert 'advisories' in json.loads(result['copy_text'])
    again = service.import_reply(session, request('read', {'path': 'greeter.py'}, 'second'))
    assert 'advisories' not in json.loads(again['copy_text'])
    task_id = service.status(session)['task']['id']
    new = service.handoff(session, True)['session_id']
    state = service.status(new)
    assert state['task']['id'] == task_id and state['session']['mode'] == 'code'
    assert not state['session']['todo_ready'] and not state['session']['commands_allowed']
    assert new in service.starter(new)['copy_text']


def test_patch_queue_pause_review_and_resume(ws, session):
    """Every patch pauses the reply until review/application, then resumes only later unexecuted tools."""
    service = ready(ws, session)
    source = ws.root / 'greeter.py'
    before = source.read_text('utf-8')
    after = before.replace('Hello {name}', 'Hello, {name}!')
    snapshot = RepositoryTools(ws).snapshot()
    proposal = {'base_snapshot': snapshot['id'], 'base_hashes': {'greeter.py': snapshot['hashes']['greeter.py']},
                'diff': ''.join(difflib.unified_diff(before.splitlines(True), after.splitlines(True), 'a/greeter.py', 'b/greeter.py'))}
    text = fenced('propose_patch', proposal, 'patch') + '\n' + fenced('todo', {'progress': {'decisions': ['Applied only after approval']}}, 'after')
    pending = service.import_reply(session, text)
    assert pending['status'] == 'awaiting_approval' and source.read_text('utf-8') == before
    assert pending['remaining_request_ids'] == ['after']
    service.review(session, pending['pending']['patch_id'])
    result = service.resolve_reply(session, pending['reply_id'], True)
    assert result['status'] == 'completed' and source.read_text('utf-8') == after
    assert 'Applied only after approval' in service.status(session)['task']['decisions']


def test_legacy_session_defaults_and_interrupted_reply_never_resumes(ws, session):
    """Existing private records gain read defaults, while running journals are not silently restarted."""
    raw = ws.get('session', session)
    for key in ['mode', 'context_limit', 'commands_allowed']:
        raw.pop(key, None)
    ws.put('session', session, raw)
    assert SessionStore(ws).get(session)['mode'] == 'plan'
    service = ready(ws, session)
    text = request('read', {'path': 'greeter.py'}, 'lost')
    result = service.import_reply(session, text)
    record = ws.get('reply', session + ':' + result['reply_id'])
    record['status'] = 'processing'
    ws.put('reply', session + ':' + result['reply_id'], record)
    interrupted = service.import_reply(session, text)
    assert interrupted['error']['code'] == 'request_interrupted' and interrupted['copy_text']


def test_repeated_command_failures_ignore_command_identity(ws):
    """Fresh execution IDs do not hide repeated failures against unchanged arguments/source/output."""
    from repopilotv3.integrations.monitor import Monitor

    monitor = Monitor(ws)
    warnings = []
    for index in range(4):
        result = {'command_id': str(index), 'started_at': str(index), 'status': 'failed',
                  'exit_code': 2, 'output': 'same compiler failure'}
        warnings.extend(monitor.record('run', 'run_command', {'argv': ['dotnet', 'build']}, result,
                                       'same-source', success=False)['warnings'])
    assert sum('failure' in message.lower() for message in warnings) == 1
    assert not monitor.record('run', 'run_command', {'argv': ['dotnet', 'build']}, result,
                              'changed-source', success=False)['warnings']


def test_repository_processes_never_inherit_protocol_stdin(tmp_path, monkeypatch):
    """Git/SDK/check children cannot consume or wait on a live Windows bridge input pipe."""
    import subprocess
    from repopilotv3.core import run

    seen = {}

    def execute(arguments, **options):
        """Capture the noninteractive process boundary without starting a host-dependent tool."""
        seen.update(options)
        return subprocess.CompletedProcess(arguments, 0, 'ok', '')

    monkeypatch.setattr(subprocess, 'run', execute)
    assert run(['git', 'rev-parse', '--is-inside-work-tree'], tmp_path)['exit_code'] == 0
    assert seen['stdin'] == subprocess.DEVNULL


def test_completed_command_survives_failed_source_observation(ws, session, monkeypatch):
    """A post-run indexing failure reports actual execution and never invites a duplicate mutation."""
    from repopilotv3.repository.index import RepositoryIndex

    class UnstableAfterExecution(RepositoryIndex):
        """Simulate concurrent source churn only in the command's after-capture."""

        def __init__(self, workspace):
            """Count this runner's refreshes independently of export services."""
            super().__init__(workspace)
            self.calls = 0

        def refresh(self, *args, **kwargs):
            """Allow the baseline, then reject an unstable after-state."""
            self.calls += 1
            if self.calls == 2:
                raise PilotError('Concurrent edits prevented source capture', 'unstable_snapshot')
            return super().refresh(*args, **kwargs)

    service = ready(ws, session)
    service.configure(session, commands_allowed=True)
    monkeypatch.setattr('repopilotv3.manual.commands.RepositoryIndex', UnstableAfterExecution)
    text = request('run_command', {'argv': ['python', '-c', "from pathlib import Path; Path('once.txt').write_text('done')"]}, 'observed')
    result = service.import_reply(session, text)
    data = result['results'][0]['data']
    assert result['status'] == 'failed'
    assert data['status'] == 'completed' and data['exit_code'] == 0
    assert data['source_observation_incomplete'] and 'Do not rerun' in data['next']
    assert service.import_reply(session, text)['status'] == 'failed'
    assert (ws.root / 'once.txt').read_text() == 'done'


def test_windows_supervisor_job_uses_a_real_name_and_kill_on_close(monkeypatch):
    """Current pywin32 requires a string job name; assign the supervisor before any child launch."""
    import types
    from repopilotv3.manual import commands

    calls = []

    def create_job(security, name):
        """Match pywin32's non-null string contract rather than accepting an invalid None."""
        assert security is None and isinstance(name, str) and name.startswith('RepoPilotV3-')
        return 'handle'

    api = types.SimpleNamespace(GetCurrentProcess=lambda: 'current')
    job = types.SimpleNamespace(CreateJobObject=create_job,
        QueryInformationJobObject=lambda *args: {'BasicLimitInformation': {'LimitFlags': 0}},
        SetInformationJobObject=lambda *args: calls.append(('limits', args)),
        AssignProcessToJobObject=lambda *args: calls.append(('assign', args)),
        JobObjectExtendedLimitInformation=9, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE=8192)
    monkeypatch.setattr(commands, 'os', types.SimpleNamespace(name='nt'))
    monkeypatch.setitem(sys.modules, 'win32api', api)
    monkeypatch.setitem(sys.modules, 'win32job', job)
    before = len(commands.WINDOWS_JOBS)
    commands.supervise_windows_tree()
    assert calls[0][1][2]['BasicLimitInformation']['LimitFlags'] == 8192
    assert calls[1] == ('assign', ('handle', 'current'))
    del commands.WINDOWS_JOBS[before:]


def test_command_facts_refresh_unchanged_source_and_continue_with_task(ws, session):
    """A read-only build/check command still updates evidence packets and survives a fresh chat."""
    from pathlib import Path

    service = ready(ws, session)
    service.configure(session, commands_allowed=True)
    before = service.prepare(session)
    result = service.import_reply(session, request('run_command', {'argv': ['python', '-c', "print('checked current source')"]}, 'fact'))
    record = result['results'][0]['data']
    after = service.prepare(session)
    assert before['manifest']['snapshot'] == after['manifest']['snapshot']
    assert before['manifest']['id'] != after['manifest']['id']
    evidence = (Path(after['directory']) / '04_VALIDATION_EVIDENCE.md').read_text('utf-8')
    assert record['command_id'] in evidence and 'executed-test proof' in evidence
    new = service.handoff(session, True)
    assert record['command_id'] in new['copy_text']
    assert not service.sessions.get(new['session_id'])['commands_allowed']
