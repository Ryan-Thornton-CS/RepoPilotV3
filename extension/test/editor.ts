/** Real extension-host checks, run identically on VS Code 1.85.2 and stable. */
import * as assert from 'node:assert/strict';
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { ReviewDocuments } from '../src/diffs';
import { deactivate, dirtyAffectedEditors, SessionPanel } from '../src/extension';
import { BackendClient } from '../src/backend';
import { backendPython } from '../src/runtime';
import { ProjectWorkspaces } from '../src/workspaces';

/** Verify stable native APIs, extension discovery and an actual local backend round trip. */
export async function run(): Promise<void> {
  const extension = vscode.extensions.getExtension('repopilot-local.repopilotv3');
  assert.ok(extension);
  await extension.activate();
  const commands = await vscode.commands.getCommands(true);
  assert.ok(commands.includes('repopilotv3.open'));
  const root = vscode.workspace.workspaceFolders![0].uri.fsPath;
  const python = await backendPython('bundled', extension.extensionUri.fsPath);
  const client = new BackendClient(root, python, '', () => {});
  try {
    await client.connect();
    await client.request('setup');
    const status = await client.request('sessions.create');
    assert.equal(status.task, null); assert.equal(status.session.name, 'New session');
    const session = status.session.id;
    const starter = await client.request('prompts.starter', { session_id: session });
    assert.ok(starter.copy_text.includes(session) && !starter.copy_text.includes('run_command'));
    const projectPrompt = await client.request('prompts.project');
    assert.ok(projectPrompt.copy_text.includes('run_command') && projectPrompt.copy_text.length <= projectPrompt.max_characters && projectPrompt.max_characters === 8000);
    const frame = (tool: string, args: any, id: string): string => JSON.stringify({ protocol_version: '1.0', request_id: id, tool, arguments: args });
    const initial = 'Planning this fixture.\n```json\n' + frame('task', { objective: 'Editor fixture', chat_name: 'Native chat' }, 'task') + '\n' + frame('todo', {}, 'todo') + '\n```';
    assert.equal((await client.request('replies.import', { session_id: session, text: initial })).status, 'completed');
    const history = await client.request('history.list', { session_id: session });
    const final = history.find((entry: any) => entry.id.startsWith('reply-result:'));
    const copied = await client.request('history.copy_turn', { session_id: session, entry_id: final.id });
    const results = copied.copy_data.results;
    assert.deepEqual(results.map((result: any) => result.request_id), ['task', 'todo']);
    assert.match((await client.request('prompts.read', { role: 'implementation' })).text, /Implementation/);
    fs.writeFileSync(path.join(root, 'alpha.py'), 'value = 1\n');
    fs.writeFileSync(path.join(root, 'beta.py'), 'value = 1\n');
    const first = (await client.request('manual.execute', { session_id: session, text: frame('read', { path: 'alpha.py' }, 'read-alpha') })).data;
    const second = (await client.request('manual.execute', { session_id: session, text: frame('read', { path: 'beta.py' }, 'read-beta') })).data;
    const pending = await client.request('replies.import', { session_id: session, text: frame('propose_patch', {
      base_snapshot: second.snapshot, base_hashes: { 'alpha.py': first.hash, 'beta.py': second.hash }, files: { 'alpha.py': 'value = 2\n', 'beta.py': 'value = 2\n' }
    }, 'two-file-patch') });
    const actualDocuments = new ReviewDocuments();
    const scheme = 'repopilotv3-native-diffs';
    const nativeRegistration = vscode.workspace.registerTextDocumentContentProvider(scheme, {
      provideTextDocumentContent: uri => actualDocuments.provideTextDocumentContent(uri.with({ scheme: 'repopilotv3-review' }))
    });
    const documentAdapter = {
      uri: (chat: string, patch: string, file: any, side: 'original' | 'proposed') => actualDocuments.uri(chat, patch, file, side).with({ scheme })
    } as ReviewDocuments;
    const panel: any = new SessionPanel({ extensionUri: extension.extensionUri } as vscode.ExtensionContext, documentAdapter);
    panel.selected = session;
    try {
      await panel.review(client, session, pending.pending.patch_id);
      const tabs = vscode.window.tabGroups.all.flatMap(group => group.tabs);
      assert.ok(tabs.some(tab => tab.label.includes('alpha.py')));
      assert.ok(tabs.some(tab => tab.label.includes('beta.py')));
      assert.equal((await client.request('replies.resolve', { session_id: session, reply_id: pending.reply_id, approved: true })).status, 'completed');
      assert.equal(fs.readFileSync(path.join(root, 'alpha.py'), 'utf8'), 'value = 2\n');
    } finally { nativeRegistration.dispose(); panel.dispose(); }
    const command = await client.request('replies.import', { session_id: session, text: frame('run_command', { argv: ['python', '-c', "print('native editor command')"] }, 'command') });
    assert.equal(command.status, 'awaiting_permission');
    const executed = await client.request('replies.resolve', { session_id: session, reply_id: command.reply_id, approved: true });
    assert.equal(executed.results[0].data.status, 'completed');
    assert.equal((await client.request('sessions.status', { session_id: status.session.id })).session.id, status.session.id);
    const provider = new ReviewDocuments();
    const registration = vscode.workspace.registerTextDocumentContentProvider('repopilotv3-review-test', {
      provideTextDocumentContent: () => 'read-only fixture'
    });
    try {
      const fixture = { path: 'sample.py', original: 'old\r\n', proposed: 'new\r\n', status: 'modified' };
      // The extension registered its own provider during activation; use a second scheme for isolation.
      const oldUri = provider.uri('chat-test', 'patch-test', fixture, 'original').with({ scheme: 'repopilotv3-review-test' });
      const newUri = oldUri.with({ path: oldUri.path.replace('/original/', '/proposed/') });
      const document = await vscode.workspace.openTextDocument(oldUri);
      assert.equal(document.getText(), 'read-only fixture');
      await assert.rejects(Promise.resolve(vscode.workspace.fs.writeFile(oldUri, Buffer.from('must fail'))));
      await vscode.commands.executeCommand('vscode.diff', oldUri, newUri, 'RepoPilot read-only test');
      const file = vscode.Uri.file(path.join(root, 'sample.py'));
      fs.writeFileSync(file.fsPath, 'original\r\n');
      const source = await vscode.workspace.openTextDocument(file);
      const edit = new vscode.WorkspaceEdit(); edit.insert(file, new vscode.Position(0, 0), '# unsaved\n');
      assert.ok(await vscode.workspace.applyEdit(edit));
      assert.ok(source.isDirty);
      assert.equal(dirtyAffectedEditors(root, ['sample.py']).length, 1);
      assert.equal(dirtyAffectedEditors(root, ['unrelated.py']).length, 0);
      await source.save();
      assert.equal(dirtyAffectedEditors(root, ['sample.py']).length, 0);
      const clipboard = 'RepoPilotV3 native clipboard Ω';
      await vscode.env.clipboard.writeText(clipboard);
      assert.equal(await vscode.env.clipboard.readText(), clipboard);
      await vscode.commands.executeCommand('repopilotv3.open');
      assert.ok(!fs.existsSync(path.join(root, '.github')));
      assert.ok(!fs.existsSync(path.join(root, '.mcp.json')));
      await client.request('sessions.update', { session_id: session, archived: true });
      assert.ok((await client.request('sessions.list', { archived: true })).some((row: any) => row.id === session));
      await client.request('sessions.update', { session_id: session, archived: false });
      assert.ok((await client.request('sessions.delete', { session_id: session })).deleted);
      assert.ok(!(await client.request('sessions.list')).some((row: any) => row.id === session));
      const nested = path.join(root, 'Projects Ω', 'Selected project');
      fs.mkdirSync(nested, { recursive: true });
      fs.writeFileSync(path.join(nested, 'only_here.py'), '"""Selected project source."""\n');
      const selectedProject = await new ProjectWorkspaces().restore(vscode.Uri.file(nested).toString(), vscode.workspace.workspaceFolders);
      assert.equal(selectedProject.uri.fsPath, nested);
      const projectClient = new BackendClient(selectedProject.uri.fsPath, python, '', () => {});
      try {
        await projectClient.connect(); await projectClient.request('setup');
        const chat = (await projectClient.request('sessions.create')).session.id;
        const packet = await projectClient.request('prompts.starter', { session_id: chat });
        assert.match(packet.copy_text, /Project: `Selected project`/);
        assert.ok(!packet.copy_text.includes('alpha.py'));
      } finally { await projectClient.close(); }
      const harnessPreview = await client.request('harness.preview', { server_name: 'repopilotv3-native-test' });
      assert.ok(!harnessPreview.conflicts.length && !fs.existsSync(path.join(root, '.github')));
      await client.request('harness.apply', { plan_id: harnessPreview.plan_id });
      assert.ok(fs.existsSync(path.join(root, '.github/agents/repopilotv3-code.agent.md')));
      assert.ok(!fs.existsSync(path.join(root, '.mcp.json')));
      assert.ok(commands.includes('repopilotv3.enableMcp') && commands.includes('repopilotv3.generateHarness'));
      if (typeof (vscode as any).lm?.registerMcpServerDefinitionProvider === 'function') {
        await vscode.commands.executeCommand('repopilotv3.enableMcp');
        assert.ok(!fs.existsSync(path.join(root, '.vscode/mcp.json')));
      }
    } finally { registration.dispose(); }
  } finally {
    await client.close();
    await vscode.commands.executeCommand('workbench.action.closeAllEditors');
    await deactivate();
  }
  if (process.env.REPOPILOTV3_EDITOR_RESULT) fs.writeFileSync(process.env.REPOPILOTV3_EDITOR_RESULT, JSON.stringify({ passed: true, version: vscode.version }));
  console.log(`RepoPilotV3 extension-host checks passed on VS Code ${vscode.version}`);
}
