/** Headless transport, rendering and trust-boundary tests; no model connection. */
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';
import { execFileSync } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { panelMessage, validSourcePath, requireTrusted } from '../src/protocol';
import { canonicalPath, samePath } from '../src/paths';
import { BackendClient } from '../src/backend';

const python = process.env.REPOPILOTV3_PYTHON || path.resolve(__dirname, '../../../.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');

/** Exercise both view-to-host validation and source-link confinement. */
test('closed messages, trust, bounded inputs and safe source paths', () => {
  assert.ok(panelMessage({ action: 'run', text: '{}' }));
  for (const value of [null, [], { action: 'shell' }, { action: 'run' }, { action: 'run', text: '{}', shell: true },
    { action: 'source', path: 'x', line: -1 }, { action: 'draft', text: 'missing session' }]) {
    assert.equal(panelMessage(value), undefined);
  }
  for (const file of ['/root', '../x', 'C:/x', 'a\\b', '.repopilotv2/config.json', 'x\0y', '.REPOPILOTV3/config.py', '.git./config.py', 'NUL.py']) assert.equal(validSourcePath(file), false);
  assert.ok(validSourcePath('Space Ω/file.py'));
  assert.throws(() => requireTrusted(false), /Trust/);
  requireTrusted(true);
});

/** Use a real Python backend to verify handshake, persistent chats and request deduplication. */
test('local RPC restores history/drafts, deduplicates requests, and does not replay', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot V3 Ω-'));
  fs.writeFileSync(path.join(root, 'hello.py'), '"""Synthetic source."""\ndef hello():\n    """Return a value."""\n    return 42\n');
  const log: string[] = [];
  let client = new BackendClient(root, python, '', text => log.push(text));
  try {
    assert.equal((await client.connect()).initialized, false);
    await client.request('setup');
    const projectPrompt = await client.request('prompts.project');
    assert.ok(projectPrompt.text.includes('Registered tools'));
    assert.ok(projectPrompt.characters === projectPrompt.copy_text.length && projectPrompt.characters <= projectPrompt.max_characters && projectPrompt.max_characters === 8000);
    assert.equal((await client.request('sessions.list')).length, 0);
    const status = await client.request('sessions.create', { objective: 'Read a value', name: 'Example Ω' });
    const session = status.session.id;
    const params = { session_id: session };
    await client.request('draft.save', { ...params, text: 'never execute this draft' });
    const text = JSON.stringify({ protocol_version: '1.0', request_id: 'start', tool: 'todo', arguments: {} });
    const first = await client.request('manual.execute', { ...params, text });
    assert.equal(first.ok, true);
    assert.deepEqual(await client.request('manual.execute', { ...params, text }), first);
    assert.equal((await client.request('history.list', params)).filter((r: any) => r.kind === 'request').length, 1);
    await assert.rejects(client.request('shell', {}), /Unknown/);
    client.dispose();
    client = new BackendClient(root, python, '', text => log.push(text));
    await client.connect();
    assert.equal((await client.request('sessions.selected')).session_id, session);
    assert.equal((await client.request('sessions.status', params)).draft, 'never execute this draft');
    assert.equal((await client.request('history.list', params)).filter((r: any) => r.kind === 'request').length, 1);
  } finally { client.dispose(); }
});

/** Render actual panel JavaScript in a DOM with a fake VS Code bridge. */
test('panel paste/run/copy, safe text rendering and passive draft restoration', () => {
  const { JSDOM } = require('jsdom');
  const htmlSource = fs.readFileSync(path.resolve(__dirname, '../../src/extension.ts'), 'utf8');
  const html = htmlSource.slice(htmlSource.indexOf('<!DOCTYPE html>'), htmlSource.indexOf('</html>') + 7);
  const dom = new JSDOM(html.replace(/<script[^>]*src[^>]*><\/script>/g, ''), { runScripts: 'outside-only' });
  const messages: any[] = [];
  let state: any = { drafts: {}, session: null };
  dom.window.acquireVsCodeApi = () => ({ postMessage: (m: any) => messages.push(m), getState: () => state, setState: (s: any) => { state = s; } });
  dom.window.eval(fs.readFileSync(path.resolve(__dirname, '../../media/panel.js'), 'utf8'));
  assert.equal(messages[0].action, 'ready');
  const dispatch = (value: any) => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: value }));
  const malicious = '<img src=x onerror="window.pwned=true">';
  const status = { task: { objective: malicious, items: [] }, draft: 'saved draft', session: { todo_ready: false },
    metrics: { observed_estimated_tokens: 4, exchanges: 0, warning: 'estimate' }, interrupted_requests: [], checklist: [] };
  dispatch({ type: 'state', sessions: [{ id: 'A', name: 'A' }], selected: 'A', status,
    history: [{ id: 'result:x', sequence: 1, at: new Date().toISOString(), kind: 'result', body: { ok: true, data: { value: malicious } } }] });
  assert.equal(dom.window.document.getElementById('request').value, 'saved draft');
  assert.equal(messages.filter(m => m.action === 'run' || m.action === 'pasteRun').length, 0);
  assert.equal(dom.window.document.querySelectorAll('img').length, 0);
  dom.window.document.getElementById('paste').click();
  assert.equal(messages.at(-1).action, 'pasteRun');
  dom.window.document.getElementById('request').value = '{"tool":"todo"}';
  dom.window.document.getElementById('run').click();
  assert.equal(messages.at(-1).action, 'run');
  dom.window.document.querySelector('#entries button').click();
  assert.equal(messages.at(-1).action, 'copy');
  assert.equal(messages.at(-1).entryId, 'result:x');
  const existingCard = dom.window.document.querySelector('#entries article');
  dispatch({ type: 'state', sessions: [{ id: 'A', name: 'A' }], selected: 'A', status, history: [] });
  assert.equal(dom.window.document.querySelector('#entries article'), existingCard);
  assert.equal(dom.window.document.getElementById('request').value, '{"tool":"todo"}');
  dom.window.close();
  // A new panel instance sees its persisted draft even when the selected ID is unchanged.
  const restored = new JSDOM(html.replace(/<script[^>]*src[^>]*><\/script>/g, ''), { runScripts: 'outside-only' });
  restored.window.acquireVsCodeApi = () => ({ postMessage: (m: any) => messages.push(m), getState: () => state, setState: (s: any) => { state = s; } });
  restored.window.eval(fs.readFileSync(path.resolve(__dirname, '../../media/panel.js'), 'utf8'));
  const runsBefore = messages.filter(m => m.action === 'run' || m.action === 'pasteRun').length;
  restored.window.dispatchEvent(new restored.window.MessageEvent('message', { data: { type: 'state', sessions: [{ id: 'A', name: 'A' }], selected: 'A', status, history: [] } }));
  assert.equal(restored.window.document.getElementById('request').value, '{"tool":"todo"}');
  assert.equal(messages.filter(m => m.action === 'run' || m.action === 'pasteRun').length, runsBefore);
  restored.window.close();
});

/** One Copy covers a growing harness turn while child controls and expanded details survive refresh. */
test('panel consolidates tool events into one copyable turn', () => {
  const { JSDOM } = require('jsdom');
  const source = fs.readFileSync(path.resolve(__dirname, '../../src/extension.ts'), 'utf8');
  const html = source.slice(source.indexOf('<!DOCTYPE html>'), source.indexOf('</html>') + 7);
  const dom = new JSDOM(html.replace(/<script[^>]*src[^>]*><\/script>/g, ''), { runScripts: 'outside-only' });
  const sent: any[] = [];
  dom.window.acquireVsCodeApi = () => ({ postMessage: (value: any) => sent.push(value), getState: () => null, setState() {} });
  dom.window.eval(fs.readFileSync(path.resolve(__dirname, '../../media/panel.js'), 'utf8'));
  const status = { session: { name: 'Chat' }, draft: '', task: null, metrics: { observed_estimated_tokens: 0, exchanges: 0 }, checklist: [] };
  const entries = [
    { sequence: 1, id: 'model', kind: 'model', body: { text: 'Inspect two files', raw: 'Full model reply' } },
    { sequence: 2, id: 'request-a', kind: 'request', body: { request_id: 'a', text: '{"tool":"read"}' } },
    { sequence: 3, id: 'result-a', kind: 'result', body: { request_id: 'a', data: { path: 'a.py', text: 'A' } } },
    { sequence: 4, id: 'result-b', kind: 'result', body: { request_id: 'b', error: { code: 'missing', message: 'B missing' } } },
    { sequence: 5, id: 'final', kind: 'harness', body: { text: 'Full reply ready' } }
  ];
  const dispatch = (history: any[]) => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'state', selected: 'A', sessions: [{ id: 'A' }], status, history } }));
  dispatch(entries.slice(0, 3));
  const document = dom.window.document;
  const details = document.querySelector('[data-entry-id="request-a"] details'); details.open = true;
  dispatch(entries.slice(3));
  const turns = [...document.querySelectorAll('#entries > article')];
  assert.equal(turns.length, 2);
  const harness = turns[1];
  const copies = [...harness.querySelectorAll('button')].filter((button: any) => button.textContent === 'Copy');
  assert.equal(copies.length, 1);
  assert.equal(harness.querySelectorAll('.turn-entry').length, 4);
  assert.equal(document.querySelector('[data-entry-id="request-a"] details'), details);
  assert.equal(details.open, true);
  copies[0].click(); assert.equal(sent.at(-1).action, 'copy'); assert.equal(sent.at(-1).entryId, 'request-a');
  assert.match(harness.textContent, /B missing/);
  dom.window.close();
});

/** A failed process cannot trigger an automatic replay of a potentially mutating request. */
test('connection loss rejects pending work; explicit reconnect inspects interruption', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot interrupted-'));
  fs.writeFileSync(path.join(root, 'code.py'), '"""Fixture."""\n');
  let client = new BackendClient(root, python, '', () => {});
  try {
    await client.connect(); await client.request('setup');
    const status = await client.request('sessions.create', { objective: 'No replay' });
    const session = status.session.id;
    const request = { protocol_version: '1.0', request_id: 'start', tool: 'todo', arguments: {} };
    await client.request('manual.execute', { session_id: session, text: JSON.stringify(request) });
    // A locally authored profile provides time to interrupt after the reservation is written.
    const configFile = path.join(root, '.repopilotv3', 'config.json');
    const config = JSON.parse(fs.readFileSync(configFile, 'utf8'));
    config.profiles.slow = { kind: 'check', args: [], timeout: 5, argv: [python, '-c', 'import time; time.sleep(1)'] };
    fs.writeFileSync(configFile, JSON.stringify(config));
    const pending = client.request('manual.execute', { session_id: session,
      text: JSON.stringify({ ...request, request_id: 'uncertain', tool: 'validate', arguments: { profile: 'slow' } }) });
    const rejected = assert.rejects(pending, /disconnected/);
    // Wait until the original request is actually persisted, not an arbitrary timer.
    for (let i = 0; i < 100; i++) {
      const current = await client.request('sessions.status', { session_id: session });
      if (current.interrupted_requests.includes('uncertain')) { break; }
      await new Promise(resolve => setTimeout(resolve, 10));
    }
    (client as any).process.kill('SIGKILL');
    await rejected;
    client.dispose();
    client = new BackendClient(root, python, '', () => {});
    await client.connect();
    const restored = await client.request('sessions.status', { session_id: session });
    assert.ok(restored.interrupted_requests.includes('uncertain'));
    assert.equal((await client.request('history.list', { session_id: session })).filter((r: any) => r.kind === 'request').length, 2);
    // The interrupted validation was never restarted by restoration.
  } finally { client.dispose(); }
});

/** Runtime selection is offline and rejects incompatible or escaping package paths. */
test('bundled runtime selection, custom override, platform errors and confinement', async () => {
  const { backendPython } = await import('../src/runtime');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot bundled Ω-'));
  assert.equal(await backendPython('/custom/python', root), '/custom/python');
  await assert.rejects(backendPython('bundled', root), /missing/);
  fs.mkdirSync(path.join(root, 'backend', 'linux-x64'), { recursive: true });
  const interpreter = path.join(root, 'backend', 'linux-x64', 'python');
  fs.writeFileSync(interpreter, 'fixture');
  const manifest = { schema_version: '1.0', backend_version: '0.2.0', platforms: { 'linux-x64': { python: 'linux-x64/python' } } };
  const file = path.join(root, 'backend', 'manifest.json');
  fs.writeFileSync(file, JSON.stringify(manifest));
  assert.equal(await backendPython('bundled', root, 'linux', 'x64'), interpreter);
  assert.equal(await backendPython('python', root, 'linux', 'x64'), interpreter);
  await assert.rejects(backendPython('bundled', root, 'darwin', 'arm64'), /supports Windows/);
  manifest.platforms['linux-x64'].python = '../../outside';
  fs.writeFileSync(file, JSON.stringify(manifest));
  await assert.rejects(backendPython('bundled', root, 'linux', 'x64'), /Unsafe/);
  manifest.platforms['linux-x64'].python = '/outside';
  fs.writeFileSync(file, JSON.stringify(manifest));
  await assert.rejects(backendPython('bundled', root, 'linux', 'x64'), /Unsafe/);
});

/** Exercise the complete prose/tool flow against the real backend with local approval. */
test('chat starters, model tasks, modes, command permission and single-exchange imports', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot chat Ω-'));
  fs.writeFileSync(path.join(root, 'sample.py'), '"""Fixture."""\nvalue = 1\n');
  const client = new BackendClient(root, python, '', () => {});
  const wrap = (tool: string, args: any, id: string): string => '```json\n' + JSON.stringify({ protocol_version: '1.0', request_id: id, tool, arguments: args }) + '\n```';
  try {
    await client.connect(); await client.request('setup');
    const { session } = await client.request('sessions.create');
    const scoped = { session_id: session.id };
    assert.equal(session.name, 'New session');
    assert.equal((await client.request('prompts.starter', scoped)).copy_text.includes('chat_name'), true);
    const input = 'I will plan this.\n' + wrap('task', { objective: 'Check fixture', chat_name: 'Fixture' }, 'task') + '\n' + wrap('todo', {}, 'todo');
    const result = await client.request('replies.import', { ...scoped, text: input });
    assert.equal(result.status, 'completed');
    assert.deepEqual(await client.request('replies.import', { ...scoped, text: input }), result);
    const state = await client.request('sessions.status', scoped);
    assert.equal(state.metrics.events, 1);
    assert.equal(state.session.name, 'Fixture');
    const mode = await client.request('sessions.configure', { ...scoped, mode: 'code' });
    assert.equal(mode.copy_text, undefined);
    const command = wrap('run_command', { argv: ['python', '-c', "print('from command')"] }, 'command');
    const pending = await client.request('replies.import', { ...scoped, text: command });
    assert.equal(pending.status, 'awaiting_permission');
    assert.equal(pending.copy_text, '');
    const finished = await client.request('replies.resolve', { ...scoped, reply_id: pending.reply_id, approved: true });
    assert.equal(finished.results[0].data.status, 'completed');
    assert.match(finished.results[0].data.output, /from command/);
    assert.doesNotMatch(finished.results[0].data.output, /Fatal Python/);
    const prose = await client.request('prompts.compose', { ...scoped, text: 'What should we improve next?' });
    assert.match(prose.copy_text, /What should we improve next/);
    const continued = await client.request('handoff', { ...scoped, confirmed_new_chat: true });
    assert.notEqual(continued.session_id, session.id);
    const next = await client.request('sessions.status', { session_id: continued.session_id });
    assert.equal(next.session.mode, undefined); assert.equal(next.session.commands_allowed, false);
    assert.equal(next.session.todo_ready, false);
  } finally { client.dispose(); }
});

/** Inspect presentation and foreground orchestration using a minimal native-API adapter. */
test('new session has no dialogs, every file opens a permanent diff, and stale actions cannot copy', async () => {
  const Module = require('module');
  const load = Module._load;
  const calls: any[] = [];
  const clipboard: string[] = [];
  let confirmDeletion = false;
  const native: any = {
    Uri: {},
    window: { createOutputChannel: () => ({ appendLine() {}, dispose() {} }), showInputBox: () => { throw new Error('unexpected dialog'); }, showQuickPick: () => { throw new Error('unexpected dialog'); },
      showWarningMessage: async () => confirmDeletion ? 'Delete session' : undefined },
    workspace: { textDocuments: [], isTrusted: true, getConfiguration: () => ({ get: (_key: string, fallback: any) => fallback }) },
    commands: { executeCommand: async (...args: any[]) => { calls.push(args); } },
    env: { clipboard: { writeText: async (text: string) => { clipboard.push(text); } } }
  };
  Module._load = function(name: string, ...args: any[]) { return name === 'vscode' ? native : load.call(this, name, ...args); };
  let SessionPanel: any;
  try { ({ SessionPanel } = require('../src/extension')); }
  finally { Module._load = load; }
  const panel: any = new SessionPanel({}, { uri: (_session: string, _patch: string, file: any, side: string) => side + ':' + file.path });
  panel.folder = { uri: {} }; panel.refresh = async () => {};
  const requests: any[] = [];
  const client = { request: async (method: string, params?: any): Promise<any> => {
    requests.push({ method, params });
    if (method === 'sessions.create') return { session: { id: 'A' } };
    if (method === 'prompts.starter') return { copy_text: 'starter' };
    if (method === 'sessions.status') return { session: { name: params.session_id, archived: false } };
    if (method === 'sessions.list') return [];
    if (method === 'patch.review') return { files: params.path ? [{ path: params.path, original: 'before', proposed: 'after' }] : ['a.py', 'b.py', 'c.py'].map(path => ({ path })) };
    return {};
  } };
  panel.epoch = 1;
  await panel.create(client, 1);
  assert.equal(requests[0].method, 'sessions.create'); assert.equal(requests[0].params, undefined);
  assert.deepEqual(clipboard, ['starter']);
  await panel.review(client, 'A', 'P');
  assert.equal(calls.length, 3);
  assert.ok(calls.every(call => call[0] === 'vscode.diff' && call[4].preview === false));
  panel.epoch = 2;
  await panel.completed(client, 'A', { copy_text: 'old action' }, 1);
  panel.selected = 'B';
  await panel.completed(client, 'A', { copy_text: 'wrong session' }, 2);
  assert.deepEqual(clipboard, ['starter']);
  let initializations = 0;
  panel.initializeBackend = async () => { initializations++; await new Promise(resolve => setTimeout(resolve, 5)); return client; };
  const simultaneous = await Promise.all([panel.backend(), panel.backend(), panel.backend()]);
  assert.equal(initializations, 1); assert.ok(simultaneous.every(value => value === client));
  panel.backend = async () => client;
  panel.jobs = 1; // Foreground work in B must not block administration of unrelated chat A.
  await panel.handle({ action: 'archive', sessionId: 'A' });
  assert.deepEqual(requests.find(r => r.method === 'sessions.update').params, { session_id: 'A', archived: true });
  await panel.handle({ action: 'delete', sessionId: 'A' });
  assert.equal(requests.filter(r => r.method === 'sessions.delete').length, 0);
  confirmDeletion = true;
  await panel.handle({ action: 'delete', sessionId: 'A' });
  assert.equal(panel.selected, 'B');
  assert.deepEqual(requests.find(r => r.method === 'sessions.delete').params, { session_id: 'A' });
  panel.jobs = 0;
  await panel.handle({ action: 'delete', sessionId: 'B' });
  assert.equal(panel.selected, undefined);
  assert.deepEqual(clipboard, ['starter']);
  let externalDeletionRefreshes = 0;
  panel.selected = 'deleted-in-another-window'; panel.view = { visible: true };
  panel.refresh = async () => { externalDeletionRefreshes++; };
  client.request = async () => { throw new Error('session_deleted: This session was deleted.'); };
  await panel.background();
  assert.equal(externalDeletionRefreshes, 1);
  assert.equal(panel.refreshing, false);
  /** Represent actual local paths without loading VS Code outside the editor host. */
  const fileUri = (value: string): any => ({ scheme: 'file', query: '', fragment: '', fsPath: path.resolve(value), toString: () => pathToFileURL(path.resolve(value)).href });
  native.Uri = { file: fileUri, parse: (value: string) => fileUri(fileURLToPath(value)) };
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot projects Ω-'));
  const first = path.join(root, 'Project One'); const second = path.join(root, 'Project Two');
  fs.mkdirSync(first); fs.mkdirSync(second);
  native.workspace.workspaceFolders = [{ uri: fileUri(root), name: 'Parent workspace', index: 0 }];
  let chosen: string | undefined = first; let picker: any;
  native.window.showOpenDialog = async (options: any) => { picker = options; return chosen ? [fileUri(chosen)] : undefined; };
  const { ProjectWorkspaces } = require('../src/workspaces');
  const projects = new ProjectWorkspaces();
  const selected = await projects.choose({ uri: fileUri(root), name: 'Parent' });
  assert.equal(selected.uri.fsPath, first);
  assert.equal(picker.canSelectFolders, true); assert.equal(picker.canSelectFiles, false);
  assert.equal(picker.canSelectMany, false);
  assert.equal((await projects.restore(selected.uri.toString(), native.workspace.workspaceFolders)).uri.fsPath, first);
  await assert.rejects(projects.restore(fileUri(path.join(root, 'missing')).toString(), native.workspace.workspaceFolders), /unavailable/);
  const file = path.join(root, 'file.txt'); fs.writeFileSync(file, 'data');
  await assert.rejects(projects.resolve(fileUri(file)), /unavailable/);
  await assert.rejects(projects.resolve({ scheme: 'https' }), /filesystem/);
  chosen = undefined; assert.equal(await projects.choose(), undefined);
  const changes: any[] = []; let closed = 0; let restored = 0; let saved: string | undefined;
  panel.post = (message: any) => changes.push(message);
  const projectState = new Map<string, any>();
  panel.context = { workspaceState: { get: (key: string, fallback: any) => projectState.get(key) ?? fallback, update: async (key: string, value: any) => { projectState.set(key, value); if (key === 'workspace') saved = value; } } };
  panel.projects = new (require('../src/projects').ProjectRegistry)(panel.context.workspaceState);
  panel.restore = async () => { restored++; assert.equal(panel.folder.uri.fsPath, canonicalPath(first)); };
  panel.folder = { uri: fileUri(root), name: 'Parent' }; panel.selected = 'old-project-chat';
  panel.client = { close: async () => { closed++; } }; panel.refreshing = true;
  chosen = first;
  await panel.chooseWorkspace();
  assert.equal(saved, fileUri(canonicalPath(first)).toString()); assert.equal(closed, 1); assert.equal(restored, 1);
  assert.equal(panel.selected, undefined); assert.equal(panel.entries.size, 0);
  assert.equal(changes.at(-1).type, 'workspaceChanged');
  assert.equal(panel.switching, false);
  chosen = undefined; await panel.chooseWorkspace(); assert.equal(closed, 1);
  chosen = second; panel.jobs = 1;
  await assert.rejects(panel.chooseWorkspace(), /current work|current tool/);
  assert.equal(panel.folder.uri.fsPath, canonicalPath(first));
  panel.jobs = 0;
  let transfers = 0;
  client.request = async (method: string) => {
    if (method === 'prompts.project') return { text: 'Reusable project instructions' };
    if (method === 'transfers.record') transfers++;
    return {};
  };
  panel.selected = undefined;
  await panel.handle({ action: 'projectPrompt' });
  assert.equal(clipboard.at(-1), 'Reusable project instructions');
  assert.equal(transfers, 0);
  fs.rmSync(root, { recursive: true, force: true });
});

/** The bottom bar always exposes context refresh beside its meter; options remain folded. */
test('consolidated footer, mode changes, pending approvals and safe model prose', () => {
  const { JSDOM } = require('jsdom');
  const source = fs.readFileSync(path.resolve(__dirname, '../../src/extension.ts'), 'utf8');
  const html = source.slice(source.indexOf('<!DOCTYPE html>'), source.indexOf('</html>') + 7);
  const dom = new JSDOM(html.replace(/<script[^>]*src[^>]*><\/script>/g, ''), { runScripts: 'outside-only' });
  const sent: any[] = [];
  dom.window.acquireVsCodeApi = () => ({ postMessage: (value: any) => sent.push(value), getState: () => undefined, setState: () => {} });
  dom.window.eval(fs.readFileSync(path.resolve(__dirname, '../../media/panel.js'), 'utf8'));
  const document = dom.window.document;
  for (const control of document.querySelectorAll('#options button, #options label, #composer-options button, #mode option, #packages button')) {
    assert.ok(control.title.length > 15, control.textContent);
    assert.equal(control.getAttribute('aria-description'), control.title);
  }
  const bar = document.querySelector('.message-bar');
  assert.ok(bar.contains(document.getElementById('context-meter')));
  assert.ok(bar.contains(document.getElementById('update-context')));
  assert.equal(document.getElementById('options').hidden, true);
  document.getElementById('options-toggle').click();
  assert.equal(document.getElementById('options').hidden, false);
  const status = { session: { name: 'Model name', mode: 'code' }, task: { objective: 'Task', items: [{ text: 'One', status: 'active' }] }, draft: '',
    metrics: { observed_estimated_tokens: 18000, context_limit: 40000, exchanges: 2 }, checklist: [{ action: 'replace', file: 'source.md', hash: 'abc' }],
    pending_replies: [{ reply_id: 'R', pending: { kind: 'command', argv: ['dotnet', 'build'], cwd: '/workspace' } }] };
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'state', selected: 'A', sessions: [{ id: 'A', name: 'Model name' }], status,
    history: [{ sequence: 1, id: 'model:1', kind: 'model', body: { text: '<img src=x onerror=bad()>\n```json\n{"x":1}\n```' } }] } }));
  assert.equal(document.getElementById('mode'), null);
  assert.match(document.getElementById('update-context').textContent, /1/);
  assert.match(document.getElementById('context-meter').title, /Context window: 45% full/);
  assert.match(document.getElementById('context-meter').title, /18,000\/40,000 tokens used/);
  assert.equal(document.getElementById('context-progress').getAttribute('stroke-dashoffset'), '55');
  assert.equal(document.querySelectorAll('img').length, 0);
  document.querySelector('#pending button').click();
  assert.equal(sent.at(-1).action, 'resolve'); assert.equal(sent.at(-1).replyId, 'R');
  document.querySelector('[data-action=roleCopy][data-role=review]').click();
  assert.equal(sent.at(-1).action, 'roleCopy');
  assert.equal(sent.at(-1).text, 'review');
  document.getElementById('request').value = 'A user question';
  document.getElementById('send').click(); assert.equal(sent.at(-1).action, 'send');
  document.getElementById('update-context').click(); assert.equal(sent.at(-1).action, 'prepare');
  const row = document.querySelector('#sessions .session-row');
  row.children[1].click(); assert.equal(sent.at(-1).action, 'archive'); assert.equal(sent.at(-1).sessionId, 'A');
  row.children[2].click(); assert.equal(sent.at(-1).action, 'delete'); assert.equal(sent.at(-1).sessionId, 'A');
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'packages' } }));
  assert.equal(document.getElementById('packages').hidden, false);
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'sessionDeleting', session: 'A' } }));
  assert.ok(document.querySelector('#sessions button').disabled);
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'sessionDeleted', session: 'A' } }));
  // Deletion is visible before another status/history round trip completes.
  assert.equal(document.querySelector('#sessions').textContent, '');
  assert.equal(document.querySelector('#entries article'), null);
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'state', selected: null, sessions: [] } }));
  assert.equal(document.querySelector('#pending').textContent, '');
  assert.equal(document.querySelector('#request').value, '');
  assert.equal(document.querySelector('#sessions').textContent, '');
  assert.match(document.querySelector('#entries').textContent, /Start a new session/);
  const draftMessages = sent.filter(value => value.action === 'draft').length;
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'workspaceChanged', workspace: '/project/Child Ω' } }));
  assert.equal(document.getElementById('workspace-root').textContent, '/project/Child Ω');
  assert.match(document.querySelector('[data-action="projectAdd"]').title, /Child Ω/);
  assert.equal(sent.filter(value => value.action === 'draft').length, draftMessages);
  dom.window.close();
});

/** Two editor connections share writer ownership; reconnect waits for the old process to exit. */
test('concurrent backends prepare starters safely and close releases the writer', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot writers Ω-'));
  fs.writeFileSync(path.join(root, 'source.py'), '"""Fixture."""\n');
  const first = new BackendClient(root, python, '', () => {});
  const second = new BackendClient(root, python, '', () => {});
  try {
    await first.connect(); await first.request('setup'); await second.connect();
    const a = (await first.request('sessions.create')).session.id;
    const b = (await second.request('sessions.create')).session.id;
    const results = await Promise.all([first.request('prompts.starter', { session_id: a }), second.request('prompts.starter', { session_id: b })]);
    assert.ok(results.every(result => result.copy_text));
    assert.ok((await first.request('history.list', { session_id: a })).every((entry: any) => entry.body.action !== 'operation_failed'));
    await first.close();
    assert.ok((first as any).process.exitCode !== null || (first as any).process.signalCode !== null);
    assert.ok((await second.request('exports.prepare', { session_id: b })).manifest);
    await second.close();
  } finally { first.dispose(); second.dispose(); }
});

/** Killing the backend must close its supervisor pipe and prevent delayed descendant writes. */
test('backend death terminates command descendants and restoration never replays', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot lost-parent Ω-'));
  let client = new BackendClient(root, python, '', () => {});
  try {
    await client.connect(); await client.request('setup');
    const session = (await client.request('sessions.create', { objective: 'Cancellation fixture' })).session.id;
    const scope = { session_id: session };
    await client.request('manual.execute', { ...scope, text: JSON.stringify({ protocol_version: '1.0', request_id: 'todo', tool: 'todo', arguments: {} }) });
    await client.request('sessions.configure', { ...scope, commands_allowed: true });
    const ready = new Promise<void>(resolve => client.on('progress', event => { if (event.text?.includes('COMMAND_READY')) resolve(); }));
    const script = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c',\"import time; from pathlib import Path; time.sleep(2); Path('escaped.txt').write_text('bad')\"]); print('COMMAND_READY',flush=True); time.sleep(30)";
    const text = JSON.stringify({ protocol_version: '1.0', request_id: 'lost', tool: 'run_command', arguments: { argv: ['python', '-c', script] } });
    const pending = client.request('replies.import', { ...scope, text });
    const rejected = assert.rejects(pending, /disconnected/);
    let timer: ReturnType<typeof setTimeout>;
    try { await Promise.race([ready, new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('command did not become ready')), 5000); })]); }
    finally { clearTimeout(timer!); }
    (client as any).process.kill('SIGKILL'); await rejected; client.dispose();
    await new Promise(resolve => setTimeout(resolve, 2300));
    assert.equal(fs.existsSync(path.join(root, 'escaped.txt')), false);
    client = new BackendClient(root, python, '', () => {});
    await client.connect();
    assert.ok((await client.request('sessions.status', scope)).interrupted_requests.includes('lost'));
    const replay = await client.request('replies.import', { ...scope, text });
    assert.equal(replay.ok, false); assert.equal(replay.error.code, 'request_interrupted');
  } finally { client.dispose(); }
});

/** Native Windows drive spelling and filesystem aliases must bind the same canonical workspace. */
test('backend canonicalizes a workspace alias without weakening root binding', async () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot alias Ω-'));
  const root = path.join(base, 'source'); const alias = path.join(base, 'alias');
  fs.mkdirSync(root);
  try { fs.symlinkSync(root, alias, process.platform === 'win32' ? 'junction' : 'dir'); }
  catch { return; }
  const client = new BackendClient(alias, python, '', () => {});
  try {
    const hello = await client.connect();
    assert.equal(canonicalPath(hello.workspace).toLowerCase(), canonicalPath(root).toLowerCase());
    await client.request('setup');
    assert.equal((await client.request('sessions.create')).session.name, 'New session');
  } finally { client.dispose(); }
});

/** Folder-space metadata survives new registries without changing roots or deleting contents. */
test('project registry isolates canonical folders, renames and removes listings only', async () => {
  const { ProjectRegistry } = require('../src/projects');
  const records = new Map<string, any>();
  const storage = { get: (key: string, fallback: any) => records.get(key) ?? fallback,
    update: async (key: string, value: any) => { records.set(key, value); } };
  const projects = new ProjectRegistry(storage);
  const first = await projects.add('file:///projects/One%20%CE%A9', 'One Ω');
  const [second, same] = await Promise.all([projects.add('file:///projects/Two', 'Two'), projects.add('file:///projects/Two', 'Two again')]);
  assert.equal(second.id, same.id);
  assert.equal(projects.list().length, 2);
  assert.equal((await projects.add(first.uri, 'Overwrite')).name, 'One Ω');
  await projects.rename(first.id, 'Renamed');
  const restored = new ProjectRegistry(storage);
  assert.equal(restored.get(first.id).name, 'Renamed');
  assert.equal(restored.get(second.id).uri, second.uri);
  await restored.remove(first.id);
  assert.throws(() => restored.get(first.id), /unavailable/);
  assert.equal(restored.list().length, 1);
  assert.equal(records.get('project.registryInitialized'), true);
});

/** Tabs/help/context links and role prompts use explicit data-only actions and passive restore. */
test('project navigation, searchable help, context file actions and copy-only prompts', () => {
  const { JSDOM } = require('jsdom');
  const source = fs.readFileSync(path.resolve(__dirname, '../../src/extension.ts'), 'utf8');
  const html = source.slice(source.indexOf('<!DOCTYPE html>'), source.indexOf('</html>') + 7);
  const dom = new JSDOM(html.replace(/<script[^>]*src[^>]*><\/script>/g, ''), { runScripts: 'outside-only' });
  const sent: any[] = [];
  dom.window.acquireVsCodeApi = () => ({ postMessage: (value: any) => sent.push(value), getState: () => undefined, setState: () => {} });
  dom.window.eval(fs.readFileSync(path.resolve(__dirname, '../../media/panel.js'), 'utf8'));
  const d = dom.window.document;
  const dispatch = (data: any) => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data }));
  dispatch({ type: 'projects', projects: [{ id: 'P', uri: 'file:///project', name: 'Synthetic Ω' }], selectedUri: 'file:///project' });
  assert.equal(d.getElementById('project-select').value, 'P');
  d.getElementById('project-select').dispatchEvent(new dom.window.Event('change'));
  assert.equal(sent.at(-1).projectId, 'P');
  dispatch({ type: 'documentation', topics: [{ title: 'Send', text: '<script>bad()</script>' }, { title: 'TODO', text: 'Reset checklist' }] });
  d.getElementById('documentation-tab').click();
  assert.equal(d.getElementById('documentation-page').hidden, false);
  assert.equal(d.querySelectorAll('#help-topics script').length, 0);
  d.getElementById('help-search').value = 'reset'; d.getElementById('help-search').dispatchEvent(new dom.window.Event('input'));
  assert.equal(d.querySelectorAll('.help-topic').length, 1);
  d.getElementById('projects-tab').click(); d.getElementById('context-tab').click();
  assert.equal(d.getElementById('context-page').hidden, false);
  dispatch({ type: 'state', selected: 'S', sessions: [], history: [], status: { session: {}, task: null, draft: '',
    metrics: { observed_estimated_tokens: 0, exchanges: 0 }, checklist: [{ file: 'map.md', hash: 'abc', action: 'upload' }], directory: '/package' } });
  d.querySelector('#context-files button').click();
  assert.equal(sent.at(-1).action, 'contextFile'); assert.equal(sent.at(-1).path, 'map.md');
  assert.equal(d.getElementById('mode'), null);
  assert.equal(sent.filter(value => ['run', 'send', 'copy'].includes(value.action)).length, 0);
  dom.window.close();
});


/** Native identity, rather than Node's lexical spelling, must own existing and pending files. */
test('native path identities cover short-name aliases and keep repository boundaries', () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot physical identity-'));
  const root = path.join(base, 'Project Long Name');
  const other = path.join(base, 'Other Project');
  const short = path.join(base, 'PROJEC~1');
  fs.mkdirSync(root); fs.mkdirSync(other);
  const native = fs.realpathSync.native;
  (fs.realpathSync as any).native = (value: string) => value === short ? native(root) : native(value);
  try {
    assert.equal(canonicalPath(short), native(root));
    assert.equal(samePath(short, root), true);
    assert.equal(samePath(short, other), false);
    assert.equal(samePath(path.join(short, 'new.py'), path.join(root, 'new.py')), true);
  } finally { (fs.realpathSync as any).native = native; fs.rmSync(base, { recursive: true, force: true }); }
});


/** All transient menus dismiss with Escape, restore focus, and keep their own controls reachable. */
test('menus anchor below the cogwheel and Escape closes every surface without executing work', () => {
  const { JSDOM } = require('jsdom');
  const source = fs.readFileSync(path.resolve(__dirname, '../../src/extension.ts'), 'utf8');
  const html = source.slice(source.indexOf('<!DOCTYPE html>'), source.indexOf('</html>') + 7);
  const dom = new JSDOM(html.replace(/<script[^>]*src[^>]*><\/script>/g, ''), { runScripts: 'outside-only' });
  const sent: any[] = [];
  dom.window.acquireVsCodeApi = () => ({ postMessage: (value: any) => sent.push(value), getState: () => undefined, setState: () => {} });
  dom.window.eval(fs.readFileSync(path.resolve(__dirname, '../../media/panel.js'), 'utf8'));
  const d = dom.window.document;
  assert.equal(d.getElementById('options').parentElement.id, 'options-anchor');
  assert.equal(d.getElementById('options-toggle').parentElement.id, 'options-anchor');
  const css = fs.readFileSync(path.resolve(__dirname, '../../media/panel.css'), 'utf8');
  assert.match(css, /#options \{ position: absolute; top: calc\(100% \+ 4px\)/);
  const cog = d.getElementById('options-toggle');
  cog.getBoundingClientRect = () => ({ bottom: 180 });
  for (const [control, panel] of [['options-toggle', 'options'], ['sessions-toggle', 'session-drawer'], ['composer-menu', 'composer-options'], ['todo-toggle', 'todo-panel']]) {
    const opener = d.getElementById(control); opener.focus(); opener.click();
    assert.equal(d.getElementById(panel).hidden, false);
    assert.equal(opener.getAttribute('aria-expanded'), 'true');
    if (panel === 'options') assert.ok(parseFloat(d.getElementById(panel).style.maxHeight) <= dom.window.innerHeight - 192);
    d.getElementById(panel).querySelector('button,input,strong')?.focus();
    const escape = new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
    d.dispatchEvent(escape);
    assert.equal(escape.defaultPrevented, true);
    assert.equal(d.getElementById(panel).hidden, true);
    assert.equal(opener.getAttribute('aria-expanded'), 'false');
    assert.equal(d.activeElement, opener);
  }
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'packages' } }));
  d.getElementById('freshness').dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(d.getElementById('packages').hidden, true);
  assert.equal(d.activeElement.id, 'update-context');
  cog.click(); d.getElementById('title').click(); assert.equal(d.getElementById('options').hidden, true);
  cog.click(); cog.click(); assert.equal(d.getElementById('options').hidden, true);
  d.getElementById('request').focus();
  const escape = new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true, cancelable: true });
  d.getElementById('request').dispatchEvent(escape);
  assert.equal(escape.defaultPrevented, false);
  assert.equal(d.activeElement.id, 'request');
  assert.deepEqual(sent.map(value => value.action), ['ready']);
  dom.window.close();
});

/** Ring fill is bounded, while hover/accessibility preserve actual estimates above the advisory limit. */
test('context ring shows current estimates, changed limits, overflow and session isolation', () => {
  const { JSDOM } = require('jsdom');
  const source = fs.readFileSync(path.resolve(__dirname, '../../src/extension.ts'), 'utf8');
  const html = source.slice(source.indexOf('<!DOCTYPE html>'), source.indexOf('</html>') + 7);
  const dom = new JSDOM(html.replace(/<script[^>]*src[^>]*><\/script>/g, ''), { runScripts: 'outside-only' });
  const sent: any[] = [];
  dom.window.acquireVsCodeApi = () => ({ postMessage: (value: any) => sent.push(value), getState: () => undefined, setState: () => {} });
  dom.window.eval(fs.readFileSync(path.resolve(__dirname, '../../media/panel.js'), 'utf8'));
  const d = dom.window.document;
  const meter = d.getElementById('context-meter'), progress = d.getElementById('context-progress');
  const render = (tokens: number, limit: number, id = 'A') => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: {
    type: 'state', selected: id, sessions: [], history: [], status: { session: {}, task: null, draft: '', checklist: [],
      metrics: { observed_estimated_tokens: tokens, context_limit: limit, exchanges: 0 } } } }));
  assert.equal(meter.disabled, true);
  render(18000, 40000);
  assert.equal(progress.getAttribute('stroke-dashoffset'), '55');
  assert.match(meter.title, /Context window: 45% full\n18,000\/40,000 tokens used/);
  assert.match(meter.title, /actual remote usage is unknown/);
  assert.equal(meter.getAttribute('aria-label'), meter.title);
  meter.click(); assert.equal(sent.at(-1).action, 'limit');
  render(18000, 20000); assert.equal(progress.getAttribute('stroke-dashoffset'), '10');
  render(50000, 40000); assert.equal(progress.getAttribute('stroke-dashoffset'), '0');
  assert.match(meter.title, /125% full/); assert.ok(meter.classList.contains('context-full'));
  render(0, 40000, 'B'); assert.equal(progress.getAttribute('stroke-dashoffset'), '100');
  assert.equal(progress.getAttribute('stroke-opacity'), '0'); assert.equal(meter.classList.contains('context-full'), false);
  render(NaN, 0, 'B'); assert.match(meter.title, /0% full/);
  assert.equal(d.querySelectorAll('#context-meter svg').length, 1);
  dom.window.close();
});
