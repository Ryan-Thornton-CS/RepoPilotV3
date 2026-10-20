/** Explicit synthetic documentation capture fixture; never runs during extension activation. */
import * as fs from 'node:fs';
import * as path from 'node:path';
import * as vscode from 'vscode';
import { BackendClient } from '../src/backend';
import { backendPython } from '../src/runtime';

/** Seed a disposable task and leave the actual panel open for a local screenshot capture. */
export async function run(): Promise<void> {
  const extension = vscode.extensions.getExtension('repopilot-local.repopilotv3')!;
  await extension.activate();
  const root = vscode.workspace.workspaceFolders![0].uri.fsPath;
  fs.writeFileSync(path.join(root, 'greeting.py'), '"""Synthetic documentation source."""\ndef greeting():\n    """Return the greeting."""\n    return "Hello"\n');
  const client = new BackendClient(root, await backendPython('bundled', extension.extensionUri.fsPath), '', () => {});
  try {
    await client.connect(); await client.request('setup');
    const session = (await client.request('sessions.create', { name: 'Add greeting test' })).session.id;
    const request = (tool: string, args: any, id: string) => JSON.stringify({ protocol_version: '1.0', request_id: id, tool, arguments: args });
    await client.request('replies.import', { session_id: session, text: 'I will inspect the greeting and add a focused test.\n```json\n' + request('task', { objective: 'Add a greeting test', chat_name: 'Add greeting test' }, 'task') + '\n' + request('todo', { progress: { items: [{ id: 'inspect', text: 'Inspect greeting behavior', status: 'completed' }, { id: 'test', text: 'Add and validate the test', status: 'active' }] } }, 'todo') + '\n```' });
    await client.request('exports.prepare', { session_id: session });
    await vscode.commands.executeCommand('workbench.action.closeAllEditors');
    await vscode.commands.executeCommand('repopilotv3.open');
    fs.writeFileSync(process.env.REPOPILOTV3_CAPTURE_READY!, 'ready');
    for (let attempt = 0; attempt < 120 && !fs.existsSync(process.env.REPOPILOTV3_CAPTURE_DONE!); attempt++) {
      await new Promise(resolve => setTimeout(resolve, 500));
    }
  } finally { await client.close(); }
}
