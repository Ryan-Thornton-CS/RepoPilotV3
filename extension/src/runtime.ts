/** Resolve packaged runtimes without installing software, reading PATH, or contacting a feed. */
import * as fs from 'fs/promises';
import * as path from 'path';
import { constants } from 'fs';

/** Read a safe platform-specific Python entry from the installed extension's manifest. */
export async function backendPython(configured: string, extensionRoot: string,
                                    platform = process.platform, architecture = process.arch): Promise<string> {
  if (configured && configured !== 'bundled' && configured !== 'python') { return configured; }
  const root = path.join(extensionRoot, 'backend');
  let manifest: any;
  try { manifest = JSON.parse(await fs.readFile(path.join(root, 'manifest.json'), 'utf8')); }
  catch { throw new Error('Bundled backend is missing. Reinstall the complete RepoPilotV3 VSIX.'); }
  if (manifest.schema_version !== '1.0' || !/^0\.2\./.test(manifest.backend_version)) {
    throw new Error('The bundled backend is incompatible. Reinstall the matching RepoPilotV3 VSIX.');
  }
  const item = manifest.platforms?.[`${platform}-${architecture}`];
  if (!item) { throw new Error(`Bundled Python supports Windows x64 and Linux/WSL x64; this host is ${platform}-${architecture}.`); }
  if (typeof item.python !== 'string' || path.isAbsolute(item.python) || item.python.includes('\\') || item.python.includes(':') ||
      item.python.split('/').some((part: string) => part === '..')) { throw new Error('Unsafe bundled runtime path rejected.'); }
  const executable = path.join(root, item.python);
  const actual = await fs.realpath(executable);
  const actualRoot = await fs.realpath(root);
  if (path.relative(actualRoot, actual).startsWith('..') || path.isAbsolute(path.relative(actualRoot, actual))) {
    throw new Error('Bundled runtime escapes extension storage. Reinstall the complete VSIX.');
  }
  if (platform !== 'win32') {
    try { await fs.access(executable, constants.X_OK); }
    catch { await fs.chmod(executable, 0o755); }
  }
  return executable;
}
