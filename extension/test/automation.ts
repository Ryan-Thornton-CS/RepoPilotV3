/** Chat automation, continuation and project-removal regressions with explicit native actions. */
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';

const Module = require('node:module');
const load = Module._load;
let confirmation: (text: string, ...choices: any[]) => Promise<string | undefined> = async () => undefined;
const copied: string[] = [];
const native: any = {
  window: { createOutputChannel: () => ({ appendLine() {}, dispose() {} }),
    showInformationMessage: (...args: any[]) => confirmation(args[0], ...args.slice(1)),
    showWarningMessage: (...args: any[]) => confirmation(args[0], ...args.slice(1)) },
  workspace: { isTrusted: true, getConfiguration: () => ({ get: (_key: string, fallback: any) => fallback }) },
  env: { clipboard: { writeText: async (text: string) => { copied.push(text); } } }
};
Module._load = function(name: string, ...args: any[]) { return name === 'vscode' ? native : load.call(this, name, ...args); };
let SessionPanel: any;
try { ({ SessionPanel } = require('../src/extension')); } finally { Module._load = load; }

/** Build a root-bound fake panel with observable RPC traffic and no external services. */
function fixture() {
  const records = new Map<string, any>();
  const events: any[] = [], calls: any[] = [];
  const context = { workspaceState: { get: (key: string, fallback: any) => records.get(key) ?? fallback,
    update: async (key: string, value: any) => { records.set(key, value); } } };
  const panel: any = new SessionPanel(context, {});
  const client: any = { root: '/synthetic', close: async () => {}, request: async (method: string, params: any) => {
    calls.push({ method, params });
    if (method === 'sessions.status') return { session: { archived: false } };
    return {};
  } };
  panel.view = { visible: true }; panel.selected = 'A'; panel.client = client;
  panel.backend = async () => client; panel.post = (value: any) => events.push(value);
  panel.refresh = async () => events.push({ type: 'refreshed' });
  return { panel, client, records, events, calls };
}

/** Concurrent triggers coalesce; a busy writer defers publication without affecting remote receipts. */
test('automatic context coalesces, defers and retries without clipboard or transfer effects', async () => {
  const { panel, client, calls, events } = fixture();
  const before = copied.length;
  let release: (value: any) => void = () => {};
  let started: () => void = () => {};
  const ready = new Promise<void>(resolve => { started = resolve; });
  const pending = new Promise(resolve => { release = resolve; });
  const original = client.request;
  client.request = async (method: string, params: any) => {
    if (method === 'exports.prepare') { calls.push({ method, params }); started(); return pending; }
    return original(method, params);
  };
  const first = panel.background(); await ready;
  await panel.background(); assert.equal(calls.filter(c => c.method === 'exports.prepare').length, 1);
  release({ deferred: true }); await first;
  assert.equal(panel.packageStates.get('A').state, 'queued');
  client.request = original; await panel.background();
  assert.equal(panel.packageStates.get('A').state, 'current');
  assert.ok(events.some(e => e.type === 'refreshed'));
  assert.equal(copied.length, before);
  assert.ok(calls.every(c => !c.method.startsWith('transfers.') && !c.method.startsWith('manual.')));
  client.request = async () => { throw new Error('Local disk unavailable'); };
  await panel.background(); assert.equal(panel.packageStates.get('A').state, 'failed');
  assert.match(events.at(-1).error, /Local disk/);
  client.request = original; await panel.background();
  assert.equal(panel.packageStates.get('A').state, 'current');
  panel.jobs = 1; const count = calls.length; await panel.background(); assert.equal(calls.length, count);
});

/** A confirmation gate cannot create multiple replacements when clicked twice or cancelled. */
test('continuation requires confirmation and duplicate clicks create one selected handoff', async () => {
  const { panel, client, calls } = fixture();
  let confirm: (value: string | undefined) => void = () => {};
  confirmation = () => new Promise(resolve => { confirm = resolve; });
  let confirmationStarted: () => void = () => {};
  const ready = new Promise<void>(resolve => { confirmationStarted = resolve; });
  confirmation = () => { confirmationStarted(); return new Promise(resolve => { confirm = resolve; }); };
  const original = client.request;
  client.request = async (method: string, params: any) => {
    if (method === 'handoff') { calls.push({ method, params }); return params.confirmed_new_chat ? { session_id: 'B', copy_text: 'Continue saved task' } : {}; }
    return original(method, params);
  };
  const completed: any[] = [];
  panel.completed = async (_client: any, id: string, result: any) => completed.push({ id, result });
  panel.background = async () => {};
  const first = panel.handle({ action: 'handoff' }); await ready;
  await panel.handle({ action: 'handoff' });
  assert.equal(calls.filter(c => c.method === 'handoff').length, 1);
  confirm('New external chat is open'); await first;
  assert.equal(panel.selected, 'B'); assert.equal(completed[0].result.copy_text, 'Continue saved task');
  assert.equal(calls.filter(c => c.params?.confirmed_new_chat).length, 1);
  confirmation = async () => undefined;
  await panel.handle({ action: 'handoff' });
  assert.equal(panel.selected, 'B'); assert.equal(calls.filter(c => c.params?.confirmed_new_chat).length, 1);
});

/** Removing the selected space changes only registry metadata; cancellation preserves everything. */
test('project removal preserves source and private state and survives registry reconstruction', async () => {
  const { panel, client, records } = fixture();
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot remove Ω-'));
  fs.writeFileSync(path.join(root, 'source.py'), 'value = 1\r\n');
  fs.mkdirSync(path.join(root, '.repopilotv3')); fs.writeFileSync(path.join(root, '.repopilotv3/evidence.json'), '{}');
  const uri = { fsPath: root, toString: () => root };
  try {
    const project = await panel.projects.add(root, 'Project');
    panel.folder = { uri, name: 'Project' }; panel.projectState = async () => {};
    let closed = 0; client.close = async () => { closed++; };
    confirmation = async () => undefined;
    await panel.projectAction({ action: 'projectRemove', projectId: project.id });
    assert.equal(panel.projects.list().length, 1); assert.equal(closed, 0);
    confirmation = async () => 'Remove project';
    client.close = async () => { throw new Error('Previous backend still draining'); };
    await assert.rejects(panel.projectAction({ action: 'projectRemove', projectId: project.id }), /draining/);
    assert.equal(panel.projects.list().length, 1); assert.equal(panel.switching, false);
    client.close = async () => { closed++; };
    await panel.projectAction({ action: 'projectRemove', projectId: project.id });
    assert.equal(panel.selected, undefined); assert.equal(closed, 1);
    assert.equal(records.get('workspace'), undefined); assert.equal(panel.projects.list().length, 0);
    const { ProjectRegistry } = require('../src/projects');
    assert.equal(new ProjectRegistry(panel.context.workspaceState).list().length, 0);
    assert.equal(fs.readFileSync(path.join(root, 'source.py'), 'utf8'), 'value = 1\r\n');
    assert.ok(fs.existsSync(path.join(root, '.repopilotv3/evidence.json')));
  } finally { fs.rmSync(root, { recursive: true, force: true }); }
});

/** The actual presentation has fixed search headers, grouped chat controls and passive context menus. */
test('bottom controls, threshold continuation and passive refresh status render safely', () => {
  const { JSDOM } = require('jsdom');
  const source = fs.readFileSync(path.resolve(__dirname, '../../src/extension.ts'), 'utf8');
  const html = source.slice(source.indexOf('<!DOCTYPE html>'), source.indexOf('</html>') + 7);
  const dom = new JSDOM(html.replace(/<script[^>]*src[^>]*><\/script>/g, ''), { runScripts: 'outside-only' });
  const sent: any[] = [];
  dom.window.acquireVsCodeApi = () => ({ postMessage: (v: any) => sent.push(v), getState: () => undefined, setState() {} });
  dom.window.eval(fs.readFileSync(path.resolve(__dirname, '../../media/panel.js'), 'utf8'));
  const d = dom.window.document;
  const dispatch = (data: any) => dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data }));
  assert.equal(d.getElementById('help-search').closest('.search-header').parentElement.id, 'documentation-page');
  assert.equal(d.getElementById('search').closest('.search-header').nextElementSibling.id, 'sessions');
  for (const id of ['chat-toggle', 'prompts-toggle', 'execution-toggle', 'todo-toggle', 'update-context']) assert.ok(d.querySelector('footer').contains(d.getElementById(id)));
  for (const action of ['starter', 'roleCopy', 'projectPrompt', 'setup', 'todoReset', 'permission']) assert.equal(d.querySelector('#options [data-action="' + action + '"]'), null);
  assert.ok(d.querySelector('#project-menu [data-action="projectRemove"]'));
  const state = (tokens: number, limit: number) => dispatch({ type: 'state', selected: 'A', sessions: [], history: [], status: {
    session: { task_id: 'T' }, task: { items: [] }, metrics: { observed_estimated_tokens: tokens, context_limit: limit, handoff_recommended: tokens >= limit }, checklist: [], draft: '' } });
  state(39999, 40000); assert.equal(d.getElementById('continue-chat').hidden, true);
  state(40000, 40000); assert.equal(d.getElementById('continue-chat').hidden, false);
  dispatch({ type: 'continuing', session: 'A', value: true }); assert.ok(d.getElementById('continue-chat').disabled);
  dispatch({ type: 'continuing', session: 'A', value: false }); assert.equal(d.getElementById('continue-chat').disabled, false);
  state(40000, 80000); assert.equal(d.getElementById('continue-chat').hidden, true);
  const before = sent.length; d.getElementById('update-context').click(); assert.equal(sent.length, before);
  dispatch({ type: 'contextRefresh', session: 'A', state: 'failed', error: '<script>disk error</script>' });
  assert.equal(d.getElementById('retry-context').hidden, false); assert.match(d.getElementById('freshness').textContent, /disk error/);
  assert.equal(d.querySelectorAll('#freshness script').length, 0);
  dispatch({ type: 'contextRefresh', session: 'A', state: 'current' }); assert.equal(d.getElementById('retry-context').hidden, true);
  assert.equal(sent.length, before); dom.window.close();
});
