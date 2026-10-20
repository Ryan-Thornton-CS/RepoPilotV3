/** Integration setup tests with a small public-VS-Code API adapter and real filesystem data. */
import { test } from 'node:test';
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as os from 'node:os';
import { canonicalPath } from '../src/paths';
import { configuration, changeProperty, applyConfiguration } from '../src/configuration';

const Module = require('node:module');
/** Minimal public-event adapter for exercising provider change notifications. */
class Signal {
  callbacks: Function[] = [];
  event = (callback: Function) => { this.callbacks.push(callback); return { dispose() {} }; };
  fire(): void { this.callbacks.forEach(callback => callback()); }
  dispose(): void { this.callbacks = []; }
}
let provider: any;
let enclosing = '';
let responses: string[] = [];
let nativeCommands: string[] = [];
const native: any = {
  EventEmitter: Signal,
  Uri: { file: (value: string) => ({ scheme: 'file', fsPath: value, toString: () => value }) },
  workspace: { isTrusted: true, workspaceFile: undefined,
    getWorkspaceFolder: () => enclosing ? { uri: { fsPath: enclosing } } : undefined,
    onDidGrantWorkspaceTrust: () => ({ dispose() {} }),
    createFileSystemWatcher: () => ({ onDidChange() {}, onDidCreate() {}, onDidDelete() {}, dispose() {} }),
    openTextDocument: async (value: any) => value },
  commands: { getCommands: async () => nativeCommands },
  window: { showQuickPick: async (items: any[]) => {
    const answer = responses.shift();
    return typeof items[0] === 'string' ? answer : items.find(item => item.label === answer);
  }, showInformationMessage: async () => responses.shift(), showTextDocument: async (value: any) => value },
  lm: { registerMcpServerDefinitionProvider: (_id: string, value: any) => { provider = value; return { dispose() {} }; } },
  McpStdioServerDefinition: class {
    constructor(public label: string, public command: string, public args: string[], public env: any, public version: string) {}
  }
};
const loader = Module._load;
Module._load = function(name: string, ...args: any[]) { return name === 'vscode' ? native : loader.call(this, name, ...args); };
const { McpIntegration, repositoryServerName } = require('../src/mcp');
Module._load = loader;

/** Create an isolated installed-runtime fixture without launching any executable. */
function fixture(): { base: string; root: string; context: any; previews: any[]; manager: any } {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot setup Ω-'));
  const root = path.join(base, 'Project A'); fs.mkdirSync(root);
  enclosing = base; responses = []; nativeCommands = []; native.workspace.isTrusted = true;
  const python = 'runtime/python';
  fs.mkdirSync(path.join(base, 'backend/runtime'), { recursive: true });
  fs.writeFileSync(path.join(base, 'backend', python), 'bundled fixture');
  fs.writeFileSync(path.join(base, 'backend/manifest.json'), JSON.stringify({ schema_version: '1.0', backend_version: '0.2.11', platforms: { [`${process.platform}-${process.arch}`]: { python } } }));
  const state = new Map<string, any>();
  const context = { extensionUri: { fsPath: base }, globalStorageUri: { fsPath: path.join(base, 'storage') }, extension: { packageJSON: { version: '0.2.11' } }, subscriptions: [],
    workspaceState: { get: (key: string, fallback: any) => state.get(key) ?? fallback, update: async (key: string, value: any) => { state.set(key, value); } } };
  const previews: any[] = [];
  const manager = new McpIntegration(context, async (edits: any[]) => { previews.push(edits); return true; });
  return { base, root, context, previews, manager };
}

/** Keep JSONC comments, CRLF and unrelated tool definitions through a targeted MCP merge. */
test('MCP JSONC edits preserve unrelated text and reject malformed input', async () => {
  const before = '{\r\n // Personal server\r\n "servers": {"other":{"command":"keep"},},\r\n}\r\n';
  const after = changeProperty(before, ['servers', 'repopilot'], { command: 'C:/User Tools/python.exe', args: ['-m', 'repopilotv3'] });
  assert.ok(after.includes('// Personal server') && after.includes('\r\n'));
  assert.equal(configuration(after).servers.other.command, 'keep');
  const removed = changeProperty(after, ['servers', 'repopilot'], undefined);
  assert.ok(removed.includes('// Personal server')); assert.equal(configuration(removed).servers.other.command, 'keep');
  assert.throws(() => configuration('{broken'), /malformed/);
  assert.throws(() => configuration('{"servers":{},"servers":{}}'), /duplicate keys/);
});

/** Failed configuration writes restore prior files and stale previews perform no write. */
test('MCP configuration rechecks and rolls back approved changes', async () => {
  const base = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilot config rollback-'));
  const a = path.join(base, 'a.json'), b = path.join(base, 'b.json');
  fs.writeFileSync(a, '{}'); fs.writeFileSync(b, '{}');
  await assert.rejects(applyConfiguration([{ file: a, before: '{stale}', after: '{}' }], base), /changed after preview/);
  const promises = require('node:fs/promises'); const rename = promises.rename;
  promises.rename = async (source: string, destination: string) => { if (destination === b) { throw new Error('injected failure'); } return rename(source, destination); };
  try {
    await assert.rejects(applyConfiguration([{ file: a, before: '{}', after: '{"changed":true}' }, { file: b, before: '{}', after: '{"changed":true}' }], base), /injected/);
  } finally { promises.rename = rename; }
  assert.equal(fs.readFileSync(a, 'utf8'), '{}'); assert.equal(fs.readFileSync(b, 'utf8'), '{}');
  assert.ok(fs.readdirSync(path.join(base, 'mcp-recovery')).length);
});

/** Register two independent roots with bundled absolute executables and no eager repository setup. */
test('native provider registration is explicit, trusted and independent per project', async () => {
  const { base, root, manager } = fixture();
  assert.deepEqual(await provider.provideMcpServerDefinitions(), []);
  assert.equal(fs.existsSync(path.join(root, '.repopilotv3')), false);
  await manager.enable(root);
  const second = path.join(base, 'Project B'); fs.mkdirSync(second); await manager.enable(second);
  const definitions = await provider.provideMcpServerDefinitions();
  assert.equal(definitions.length, 2);
  assert.equal(definitions[0].label, repositoryServerName(root));
  assert.ok(path.isAbsolute(definitions[0].command));
  assert.equal(canonicalPath(definitions[0].args[definitions[0].args.indexOf('--repo') + 1]), canonicalPath(root));
  assert.equal(canonicalPath(definitions[1].args[definitions[1].args.indexOf('--repo') + 1]), canonicalPath(second));
  assert.equal(fs.existsSync(path.join(root, '.mcp.json')), false);
  native.workspace.isTrusted = false;
  assert.deepEqual(await provider.provideMcpServerDefinitions(), []);
  await assert.rejects(manager.enable(root), /Trust/);
  native.workspace.isTrusted = true; manager.dispose();
});

/** An old portable entry is retained until the developer explicitly chooses and previews migration. */
test('duplicate registration migration preserves unrelated servers and comments', async () => {
  const { base, root, manager, previews } = fixture();
  const file = path.join(base, '.mcp.json');
  fs.writeFileSync(file, '// Existing user config\n' + JSON.stringify({ mcpServers: { repopilot: { command: 'old-python', args: ['-m', 'repopilot', '--repo', root] }, other: { command: 'keep' } } }));
  responses = ['Keep existing configuration']; await manager.enable(root);
  assert.equal(manager.registrations().length, 0);
  responses = ['Switch to extension-managed setup']; await manager.enable(root);
  const text = fs.readFileSync(file, 'utf8');
  assert.ok(text.includes('// Existing user config')); assert.equal(configuration(text).mcpServers.other.command, 'keep');
  assert.equal(configuration(text).mcpServers.repopilot, undefined);
  assert.equal(previews.length, 1); assert.equal((await manager.definitions()).length, 1);
  // A subsequently added conflicting definition suppresses the provider instead of launching twice.
  fs.writeFileSync(file, changeProperty(text, ['mcpServers', 'repopilot'], { args: ['--repo', root] }));
  assert.equal((await manager.definitions()).length, 0); manager.dispose();
});

/** Portable definitions for a separate local-model client can coexist without being deleted. */
test('portable local-model config is retained when explicitly enabling a separate VS Code provider', async () => {
  const { base, root, manager, previews } = fixture();
  const file = path.join(base, '.mcp.json');
  const original = JSON.stringify({ mcpServers: { repopilot: { command: 'local-model-python', args: ['-m', 'repopilotv3', '--repo', root] } } });
  fs.writeFileSync(file, original);
  responses = ['Keep portable config and enable VS Code MCP'];
  await manager.enable(root);
  assert.equal(fs.readFileSync(file, 'utf8'), original);
  assert.equal(previews.length, 0); assert.equal((await manager.definitions()).length, 1);
  manager.dispose();
});

/** Missing APIs preserve old editor usability and only offer fallback when native MCP exists. */
test('older hosts report unsupported MCP or preview a file fallback', async () => {
  const old = native.lm; native.lm = undefined;
  try {
    const { root, manager, previews } = fixture();
    assert.equal(manager.supported, false);
    nativeCommands = ['repopilotv3.enableMcp'];
    await assert.rejects(manager.enable(root), /does not expose MCP/);
    assert.equal(fs.existsSync(path.join(root, '.vscode/mcp.json')), false);
    nativeCommands = ['workbench.mcp.listServer']; responses = ['Preview configuration'];
    await manager.enable(root);
    const file = path.join(root, '.vscode/mcp.json');
    assert.equal(previews.length, 1); assert.equal(configuration(fs.readFileSync(file, 'utf8')).servers[repositoryServerName(root)].args.at(-1), 'serve');
    const data = configuration(fs.readFileSync(file, 'utf8')); data.servers[repositoryServerName(root)].args.push('local-change'); fs.writeFileSync(file, JSON.stringify(data));
    responses = [repositoryServerName(root)]; await assert.rejects(manager.disable(), /changed/);
    manager.dispose();
  } finally { native.lm = old; }
});

/** Native details/summary controls group settings and integration actions do not require a chat. */
test('settings groups retain hover help and expose setup without a session', () => {
  const { JSDOM } = require('jsdom');
  const source = fs.readFileSync(path.resolve(__dirname, '../../src/extension.ts'), 'utf8');
  const html = source.slice(source.indexOf('<!DOCTYPE html>'), source.indexOf('</html>') + 7);
  const dom = new JSDOM(html.replace(/<script[^>]*src[^>]*><\/script>/g, ''), { runScripts: 'outside-only' });
  const sent: any[] = [];
  dom.window.acquireVsCodeApi = () => ({ postMessage: (value: any) => sent.push(value), getState: () => null, setState() {} });
  dom.window.eval(fs.readFileSync(path.resolve(__dirname, '../../media/panel.js'), 'utf8'));
  const groups = [...dom.window.document.querySelectorAll('#options > details > summary')].map((item: any) => item.textContent);
  assert.deepEqual(groups, ['Copilot & MCP', 'Projects', 'Maintenance']);
  const enable = dom.window.document.querySelector('[data-action="mcpEnable"]');
  assert.ok(enable.title); enable.click(); assert.equal(sent.at(-1).action, 'mcpEnable');
  dom.window.close();
});


/** A parent config targeting an alias is still a duplicate, and server names stay physical. */
test('MCP duplicate detection and labels share canonical repository identities', async t => {
  const { base, root, manager } = fixture();
  const alias = path.join(base, 'Alias A');
  try { fs.symlinkSync(root, alias, process.platform === 'win32' ? 'junction' : 'dir'); }
  catch { manager.dispose(); t.skip('Filesystem does not permit directory aliases'); return; }
  try {
    assert.equal(repositoryServerName(alias), repositoryServerName(root));
    fs.writeFileSync(path.join(base, '.mcp.json'), JSON.stringify({ mcpServers: { repopilot: {
      command: 'existing', args: ['-m', 'repopilotv3', '--repo', alias] } } }));
    assert.equal((await manager.existing(root)).length, 1);
    responses = ['Keep existing configuration']; await manager.enable(root);
    assert.equal(manager.registrations().length, 0);
    responses = ['Switch to extension-managed setup']; await manager.enable(alias);
    const definitions = await manager.definitions();
    assert.equal(definitions.length, 1);
    assert.equal(definitions[0].label, repositoryServerName(root));
  } finally { manager.dispose(); fs.rmSync(base, { recursive: true, force: true }); }
});
