/** Synthetic browser, picker, documentation and deterministic relay regression checks. */
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import { NativeBrowser, BrowserPreferences, BrowserSnapshot, installPicker, inspectPage, bounded } from '../src/browser';
import { BrowserRelay, RelayIO } from '../src/relay';
import { BackendClient } from '../src/backend';
import * as os from 'node:os';

/** Create an in-memory browser boundary with no model, network or editor dependency. */
function fixture(mode: 'fill' | 'capture' | 'loop') {
  const settings: BrowserPreferences = { enabled: true, mode, budget: 10, url: 'http://private/chat',
    selectors: { input: '#input', send: '#send', sent: '.user', reply: '.assistant', complete: '.assistant.done' } };
  const state: BrowserSnapshot = { url: settings.url, draft: '', busy: false, complete: false, replies: [], sent: [] };
  const records = new Map<string, any>(); const captured: any[] = []; const statuses: string[] = [];
  let sends = 0, fills = 0, active = true;
  const io: RelayIO = {
    snapshot: async () => ({ ...state, replies: [...state.replies], sent: [...state.sent] }),
    fill: async text => { fills++; state.draft = text; },
    send: async () => { sends++; state.sent.push(state.draft); state.draft = ''; state.replies.push('Complete reply'); state.complete = true; },
    journal: async (id, phase, text) => {
      if (phase !== 'inspect') records.set(id, { phase, text });
      return records.get(id) || {};
    },
    captured: async (text, automatic) => { captured.push({ text, automatic }); },
    status: text => { statuses.push(text); }, current: () => active
  };
  return { settings, state, io, records, captured, statuses, sends: () => sends, fills: () => fills, deactivate: () => { active = false; } };
}

/** Fill-only never clicks Send or imports model content. */
test('experimental relay is disarmed initially; fill-only does not send', async () => {
  const f = fixture('fill'), relay = new BrowserRelay();
  assert.equal(relay.armed, false);
  await relay.transfer(f.io, f.settings, 'one', 'text'); assert.equal(f.fills(), 0);
  await relay.start(f.io, f.settings); await relay.transfer(f.io, f.settings, 'one', 'text');
  assert.equal(f.state.draft, 'text'); assert.equal(f.sends(), 0); assert.equal(f.captured.length, 0);
  assert.equal(f.records.get('one').phase, 'filled');
  relay.pause(); assert.equal(relay.armed, false);
});

/** Capture and full-loop differ only at the explicit import boundary. */
test('send/capture and full loop acknowledge delivery before capturing', async () => {
  for (const mode of ['capture', 'loop'] as const) {
    const f = fixture(mode), relay = new BrowserRelay();
    await relay.start(f.io, f.settings); await relay.transfer(f.io, f.settings, 'one', 'exact text');
    assert.equal(f.sends(), 1); assert.equal(f.records.get('one').phase, 'sent');
    assert.deepEqual(f.captured, [{ text: 'Complete reply', automatic: mode === 'loop' }]);
    await assert.rejects(relay.transfer(f.io, f.settings, 'one', 'exact text'), /replay/);
    assert.equal(f.sends(), 1); assert.equal(relay.armed, false);
  }
});

/** No recovery path automatically repeats an uncertain click. */
test('uncertain sends, navigation, permissions and existing drafts pause without replay', async () => {
  const f = fixture('loop'), relay = new BrowserRelay();
  f.io.send = async () => { throw new Error('native permission denied or connection lost'); };
  await relay.start(f.io, f.settings);
  await assert.rejects(relay.transfer(f.io, f.settings, 'uncertain', 'result'), /permission/);
  assert.equal(f.records.get('uncertain').phase, 'send_pending');
  assert.equal(relay.armed, false);
  f.state.draft = ''; await relay.start(f.io, f.settings);
  await assert.rejects(relay.transfer(f.io, f.settings, 'uncertain', 'result'), /replay/);
  await relay.start(f.io, f.settings); f.state.url += '/other';
  await assert.rejects(relay.transfer(f.io, f.settings, 'new', 'result'), /navigation/);
  f.state.draft = 'Human draft'; await assert.rejects(relay.start(f.io, f.settings), /draft/);
});

/** An explicit incomplete flag takes precedence over a generating control disappearing. */
test('stopped partial model output is never imported by the full loop', async () => {
  const f = fixture('loop'), relay = new BrowserRelay();
  let generatedPolls = 0;
  f.io.send = async () => { f.state.sent.push(f.state.draft); f.state.draft = ''; f.state.replies.push('partial tool request'); f.state.busy = true; };
  const snapshot = f.io.snapshot;
  f.io.snapshot = async () => {
    if (f.state.replies.length && ++generatedPolls > 1) f.state.busy = false;
    return snapshot();
  };
  await relay.start(f.io, f.settings);
  await assert.rejects(relay.transfer(f.io, f.settings, 'partial', 'response'), /Partial text was not imported/);
  assert.equal(f.captured.length, 0); assert.equal(relay.armed, false);
});

/** Local pauses and budgets apply even when the model keeps returning tools. */
test('session isolation and automatic-turn budget stop the relay', async () => {
  const f = fixture('loop'), relay = new BrowserRelay(); f.settings.budget = 1;
  await relay.start(f.io, f.settings); await relay.transfer(f.io, f.settings, 'one', 'result');
  f.state.complete = false;
  await assert.rejects(relay.transfer(f.io, f.settings, 'two', 'next result'), /budget/);
  assert.equal(f.sends(), 1);
  assert.equal(f.state.draft, ''); assert.equal(f.records.has('two'), false);
  f.state.draft = ''; await relay.start(f.io, f.settings); f.deactivate();
  await assert.rejects(relay.transfer(f.io, f.settings, 'three', 'result'), /session/);
});

/** Missing browser tools are a feature limitation, never a panel startup failure. */
test('native adapter detects capabilities and invokes fixed code without a model', async () => {
  assert.equal(new NativeBrowser({}).available(), false);
  await assert.rejects(new NativeBrowser({}).pages(), /unavailable/);
  const calls: any[] = [];
  const native = new NativeBrowser({ lm: { tools: [
    { name: 'list_browser_pages' }, { name: 'run_playwright_code', inputSchema: { properties: { pageId: {}, code: {} } } }
  ], invokeTool: async (name: string, options: any) => {
    calls.push({ name, ...options });
    return { content: [{ value: name === 'list_browser_pages' ? '- [page-1] Private chat (https://private/chat) (active)' : 'Result: {"confirmed":true}\nCompleted' }] };
  } } });
  assert.equal(native.available(), true); assert.equal((await native.pages())[0].id, 'page-1');
  assert.deepEqual(await native.code('page-1', 'return {confirmed:true};'), { confirmed: true });
  assert.equal(calls.length, 2); assert.equal(calls[1].input.pageId, 'page-1');
  await assert.rejects(bounded(new Promise(() => {}), 5), /uncertain/);
});

/** Native deferred execution resumes the same result instead of repeating its mutation. */
test('native deferred browser results are awaited without replaying code', async () => {
  const inputs: any[] = [];
  const native = new NativeBrowser({ lm: { tools: [{ name: 'list_browser_pages' },
    { name: 'run_playwright_code', inputSchema: { properties: { pageId: {}, code: {} } } }],
    invokeTool: async (_: string, options: any) => { inputs.push(options.input); return { content: [{ value: inputs.length === 1 ? '[deferredResultId=existing-result] pending' : 'Result: true' }] }; }
  } });
  assert.equal(await native.code('page', 'one mutation'), true);
  assert.equal(inputs.filter(i => i.code).length, 1);
  assert.equal(inputs[1].deferredResultId, 'existing-result');
});

/** Exercise the real injected picker against DOM controls without clicking their handlers. */
test('picker highlights, selects role families and cancels on Escape', () => {
  const { JSDOM } = require('jsdom');
  const dom = new JSDOM('<button data-testid="send">Send</button><article data-role="assistant" data-complete="true">Example</article><article data-role="assistant">Other</article>', { runScripts: 'outside-only', url: 'http://127.0.0.1/chat' });
  let activated = false; dom.window.document.querySelector('button').onclick = () => { activated = true; };
  const pick = dom.window.eval('(' + installPicker.toString() + ')');
  pick({ token: 'one', family: false });
  dom.window.document.querySelector('button').click();
  assert.equal(activated, false); assert.equal(dom.window.__repopilotPicker.selector, 'button[data-testid="send"]');
  pick({ token: 'two', family: true }); dom.window.document.querySelector('article').click();
  assert.equal(dom.window.__repopilotPicker.matches, 2);
  assert.equal(dom.window.__repopilotPicker.complete, 'article[data-role="assistant"][data-complete="true"]');
  pick({ token: 'three', family: false }); dom.window.document.dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(dom.window.__repopilotPicker.cancelled, true);
  dom.window.close();
});

/** Interrupted streamed output must remain incomplete even after the Stop control disappears. */
test('demo streams canned tools and Stop leaves partial replies incomplete', async () => {
  const { JSDOM } = require('jsdom');
  const assets = path.resolve(__dirname, '../../../src/repopilotv3/browser/assets');
  const dom = new JSDOM(fs.readFileSync(path.join(assets, 'index.html'), 'utf8'), { runScripts: 'outside-only', url: 'http://127.0.0.1/' });
  dom.window.crypto.randomUUID = () => 'synthetic-run';
  dom.window.eval(fs.readFileSync(path.join(assets, 'demo.js'), 'utf8'));
  const d = dom.window.document; d.getElementById('message').value = 'start';
  d.getElementById('composer').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }));
  assert.equal(d.body.dataset.generating, 'true');
  d.getElementById('stop').click();
  await new Promise(resolve => setTimeout(resolve, 100));
  assert.equal(d.body.dataset.generating, 'false');
  assert.equal([...d.querySelectorAll('.assistant')].at(-1).dataset.complete, 'false');
  d.getElementById('reset').click(); assert.equal(d.querySelectorAll('article').length, 0);
  dom.window.close();
});

/** Run the actual scripted page and Python request pipeline together in a synthetic repository. */
test('local demo completes task/TODO and search through the full relay pipeline', async () => {
  const { JSDOM } = require('jsdom');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot relay Ω-'));
  fs.writeFileSync(path.join(root, 'README.md'), '# Synthetic relay project\n');
  const python = process.env.REPOPILOTV3_PYTHON || path.resolve(__dirname, '../../../.venv', process.platform === 'win32' ? 'Scripts/python.exe' : 'bin/python');
  const client = new BackendClient(root, python, '', () => {});
  const assets = path.resolve(__dirname, '../../../src/repopilotv3/browser/assets');
  const dom = new JSDOM(fs.readFileSync(path.join(assets, 'index.html'), 'utf8'), { runScripts: 'outside-only', url: 'http://127.0.0.1/demo' });
  dom.window.crypto.randomUUID = () => 'e2e-run';
  dom.window.setTimeout = (fn: Function) => setTimeout(fn, 1);
  dom.window.HTMLElement.prototype.getClientRects = function() { return this.hidden ? [] : [{}]; };
  dom.window.eval(fs.readFileSync(path.join(assets, 'demo.js'), 'utf8'));
  const inspect = dom.window.eval('(' + inspectPage.toString() + ')');
  const settings = fixture('loop').settings;
  settings.selectors = { input: '#message', send: '#send', sent: '[data-role="user"]', reply: '[data-role="assistant"]', complete: '[data-role="assistant"][data-complete="true"]' };
  const relay = new BrowserRelay(); let next: { id: string; text: string } | undefined; const outputs: any[] = [];
  try {
    await client.connect(); await client.request('setup');
    const session = (await client.request('sessions.create')).session.id;
    const starter = await client.request('prompts.starter', { session_id: session });
    const io: RelayIO = {
      snapshot: async () => inspect(settings.selectors),
      fill: async text => { dom.window.document.getElementById('message').value = text; },
      send: async () => { dom.window.document.getElementById('send').click(); },
      journal: (transfer_id, phase, text) => client.request('browser.transfer', { session_id: session, transfer_id, phase, ...(text ? { text } : {}) }),
      captured: async text => { const result = await client.request('replies.import', { session_id: session, text }); outputs.push(result); next = result.results?.length ? { id: result.reply_id, text: result.copy_text } : undefined; },
      status: () => {}, current: () => true
    };
    await relay.start(io, settings);
    next = { id: starter.entry_id, text: starter.copy_text };
    for (let turn = 0; next && turn < 4; turn++) { const outgoing = next; next = undefined; await relay.transfer(io, settings, outgoing.id, outgoing.text); }
    assert.equal(outputs.length, 3);
    assert.equal(outputs[0].results.length, 2);
    assert.equal(outputs[1].results[0].ok, true);
    const status = await client.request('sessions.status', { session_id: session });
    assert.equal(status.session.todo_ready, true); assert.match(status.task.objective, /browser relay/);
    assert.equal(status.metrics.exchanges, 3); assert.equal(status.running_commands.length, 0);
    assert.equal(fs.readFileSync(path.join(root, 'README.md'), 'utf8'), '# Synthetic relay project\n');
  } finally { relay.pause(); dom.window.close(); await client.close(); fs.rmSync(root, { recursive: true, force: true }); }
});

/** Search queries and repository text remain text even when they contain markup. */
test('help navigation, search highlights and removed composer override', () => {
  const { JSDOM } = require('jsdom');
  const source = fs.readFileSync(path.resolve(__dirname, '../../src/extension.ts'), 'utf8');
  const html = source.slice(source.indexOf('<!DOCTYPE html>'), source.indexOf('</html>') + 7);
  const dom = new JSDOM(html.replace(/<script[^>]*src[^>]*><\/script>/g, ''), { runScripts: 'outside-only' });
  const sent: any[] = []; dom.window.acquireVsCodeApi = () => ({ getState: () => undefined, setState() {}, postMessage: (m: any) => sent.push(m) });
  dom.window.eval(fs.readFileSync(path.resolve(__dirname, '../../media/panel.js'), 'utf8'));
  const d = dom.window.document;
  const topics = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../docs/help.json'), 'utf8'));
  const callouts = JSON.parse(fs.readFileSync(path.resolve(__dirname, '../../../docs/callouts.json'), 'utf8'));
  for (const topic of topics) topic.callouts = callouts[topic.callout_key] || [];
  dom.window.dispatchEvent(new dom.window.MessageEvent('message', { data: { type: 'documentation', topics } }));
  assert.ok(d.querySelector('#help-contents summary')); assert.match(d.querySelector('.help-topic').textContent, /1\. Add/);
  const query = d.getElementById('help-search'); query.value = 'copy'; query.dispatchEvent(new dom.window.Event('input'));
  assert.ok(d.querySelectorAll('mark').length > 1); d.getElementById('help-next-match').click();
  assert.equal(d.querySelectorAll('mark.current-match').length, 1);
  query.value = '<script>'; query.dispatchEvent(new dom.window.Event('input')); assert.equal(d.querySelector('#help-topics script'), null);
  query.value = ''; query.dispatchEvent(new dom.window.Event('input')); assert.equal(d.querySelectorAll('mark').length, 0);
  assert.equal(d.querySelector('[data-action="run"]'), null);
  const before = sent.length; d.getElementById('request').dispatchEvent(new dom.window.KeyboardEvent('keydown', { ctrlKey: true, shiftKey: true, key: 'Enter', bubbles: true }));
  assert.equal(sent.length, before);
  dom.window.close();
});
