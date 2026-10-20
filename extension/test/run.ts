/** Explicitly download/run the requested test editor; normal extension use never downloads. */
import { downloadAndUnzipVSCode } from '@vscode/test-electron';
import { spawn } from 'node:child_process';
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
  const resultFile = path.join(userData, 'editor-result.json');
  process.env.REPOPILOTV3_EDITOR_RESULT = resultFile;
  try {
    const executable = await downloadAndUnzipVSCode({ version: process.env.VSCODE_TEST_VERSION || '1.85.2',
      cachePath: path.join(extensionDevelopmentPath, '.vscode-test') });
    // Launch the executable directly: CMD shell joining breaks UNC roots and paths with spaces.
    const args = [root, '--disable-workspace-trust', '--disable-extensions', '--no-sandbox', '--disable-gpu',
      '--disable-gpu-sandbox', '--disable-dev-shm-usage', '--disable-updates', '--skip-welcome', '--skip-release-notes',
      '--user-data-dir=' + userData, '--extensions-dir=' + path.join(userData, 'extensions'),
      '--extensionDevelopmentPath=' + extensionDevelopmentPath, '--extensionTestsPath=' + path.resolve(__dirname, 'editor.js')];
    await new Promise<void>((resolve, reject) => {
      const child = spawn(executable, args, { shell: false, stdio: 'inherit', timeout: 180000 });
      child.once('error', reject);
      child.once('close', (code, signal) => {
        if (code === 0 && !signal) resolve();
        else reject(new Error(`Editor exited with code ${code}, signal ${signal ?? 'none'}`));
      });
    });
    const evidence = JSON.parse(fs.readFileSync(resultFile, 'utf8'));
    if (!evidence.passed || !evidence.version || (process.env.VSCODE_TEST_VERSION !== 'stable' && evidence.version !== (process.env.VSCODE_TEST_VERSION || '1.85.2'))) {
      throw new Error('Editor did not record successful assertions and teardown for the requested version.');
    }
  } finally {
    const proof = path.join(extensionDevelopmentPath, '.vscode-test/editor-result.json');
    fs.rmSync(proof, { force: true });
    if (fs.existsSync(resultFile)) fs.copyFileSync(resultFile, proof);
    const logs = path.join(extensionDevelopmentPath, '.vscode-test/repopilot-logs');
    if (fs.existsSync(path.join(userData, 'logs'))) {
      fs.cpSync(path.join(userData, 'logs'), logs, { recursive: true });
    }
    fs.rmSync(root, { recursive: true, force: true });
    fs.rmSync(userData, { recursive: true, force: true });
  }
}
main().catch(error => { console.error(error); process.exitCode = 1; });
