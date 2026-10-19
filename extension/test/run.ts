/** Explicitly download/run the requested test editor; normal extension use never downloads. */
import { runTests } from '@vscode/test-electron';
import * as fs from 'node:fs';
import * as os from 'node:os';
import * as path from 'node:path';

/** Launch a clean extension-development host with isolated synthetic workspace/state. */
async function main(): Promise<void> {
  // An IDE-launched shell may inherit flags that turn Electron into plain Node.
  delete process.env.ELECTRON_RUN_AS_NODE;
  for (const key of Object.keys(process.env)) {
    if (key.startsWith('VSCODE_') && key !== 'VSCODE_TEST_VERSION') { delete process.env[key]; }
  }
  const extensionDevelopmentPath = path.resolve(__dirname, '../..');
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilotV3 editor Ω-'));
  const userData = fs.mkdtempSync(path.join(os.tmpdir(), 'RepoPilotV3 editor-data-'));
  await runTests({ version: process.env.VSCODE_TEST_VERSION || '1.85.2',
    extensionDevelopmentPath, extensionTestsPath: path.resolve(__dirname, 'editor.js'),
    launchArgs: [root, '--disable-workspace-trust', '--disable-extensions', '--no-sandbox', '--disable-gpu',
      '--user-data-dir=' + userData] });
}
main().catch(error => { console.error(error); process.exitCode = 1; });
