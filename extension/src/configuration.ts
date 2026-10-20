/** JSONC-preserving MCP configuration edits with explicit previews and recovery copies. */
import * as fs from 'fs/promises';
import * as path from 'path';
import * as crypto from 'crypto';

/** Proposed whole-document change and the exact original bytes used to derive it. */
export interface ConfigurationEdit { file: string; before: string | null; after: string; }

/** Load Microsoft's pinned JSONC parser from the VSIX or development dependencies. */
function parser(): typeof import('jsonc-parser') {
  try { return require(path.join(__dirname, '../vendor/jsonc-parser/main.js')); }
  catch (error: any) { if (error.code !== 'MODULE_NOT_FOUND') { throw error; } return require('jsonc-parser'); }
}

/** Read JSONC without discarding comments, rejecting malformed configuration rather than replacing it. */
export function configuration(text: string): any {
  const errors: import('jsonc-parser').ParseError[] = [];
  const value = parser().parse(text, errors, { allowTrailingComma: true });
  if (errors.length || !value || typeof value !== 'object' || Array.isArray(value)) { throw new Error('MCP configuration is malformed; repair it before setup.'); }
  const inspect = (node: import('jsonc-parser').Node): void => {
    if (node.type === 'object') {
      const keys = (node.children ?? []).map(child => child.children?.[0]?.value);
      if (new Set(keys).size !== keys.length) { throw new Error('MCP configuration contains duplicate keys; repair them before setup.'); }
    }
    for (const child of node.children ?? []) { inspect(child); }
  };
  inspect(parser().parseTree(text)!);
  return value;
}

/** Change one JSON property while retaining unrelated formatting and comments. */
export function changeProperty(text: string, keys: string[], value: unknown): string {
  configuration(text);
  return parser().applyEdits(text, parser().modify(text, keys, value, { formattingOptions: { insertSpaces: true, tabSize: 2, eol: text.includes('\r\n') ? '\r\n' : '\n' } }));
}

/** Read a regular configuration file without following symlinked files or parent directories. */
export async function readConfiguration(file: string): Promise<string | null> {
  let current = path.resolve(file);
  while (true) {
    try { if ((await fs.lstat(current)).isSymbolicLink()) { throw new Error(`Linked configuration path rejected: ${current}`); } }
    catch (error: any) { if (error.code !== 'ENOENT') { throw error; } }
    const parent = path.dirname(current); if (parent === current) { break; } current = parent;
  }
  try { return await fs.readFile(file, 'utf8'); } catch (error: any) { if (error.code === 'ENOENT') { return null; } throw error; }
}

/** Atomically replace one configuration after its parent was checked. */
async function replace(file: string, text: string): Promise<void> {
  await fs.mkdir(path.dirname(file), { recursive: true });
  const temporary = file + '.repopilot-' + crypto.randomBytes(8).toString('hex');
  let mode: number | undefined;
  try { mode = (await fs.stat(file)).mode; } catch (error: any) { if (error.code !== 'ENOENT') { throw error; } }
  try { await fs.writeFile(temporary, text, { mode: mode ?? 0o600 }); await fs.rename(temporary, file); }
  finally { await fs.unlink(temporary).catch(() => {}); }
}

/** Recheck approved documents and keep a durable backup before changing MCP registrations. */
export async function applyConfiguration(edits: ConfigurationEdit[], storage: string): Promise<string> {
  for (const edit of edits) {
    if (await readConfiguration(edit.file) !== edit.before) { throw new Error(`Configuration changed after preview: ${edit.file}`); }
    configuration(edit.after);
  }
  const recovery = path.join(storage, 'mcp-recovery', crypto.randomBytes(12).toString('hex'));
  await fs.mkdir(recovery, { recursive: true, mode: 0o700 });
  const journal = { status: 'prepared', edits };
  await fs.writeFile(path.join(recovery, 'journal.json'), JSON.stringify(journal, null, 2), { mode: 0o600 });
  const written: ConfigurationEdit[] = [];
  try {
    for (const edit of edits) {
      if (await readConfiguration(edit.file) !== edit.before) { throw new Error(`Configuration changed during setup: ${edit.file}`); }
      written.push(edit); await replace(edit.file, edit.after);
    }
    journal.status = 'applied';
  } catch (error) {
    const failures: string[] = [];
    for (const edit of written.reverse()) {
      try {
        const current = await readConfiguration(edit.file);
        if (current === edit.before) { continue; }
        if (current !== edit.after && current !== edit.before) { throw new Error('Subsequent edit detected'); }
        if (edit.before === null) { await fs.unlink(edit.file).catch((failure: any) => { if (failure.code !== 'ENOENT') { throw failure; } }); }
        else { await replace(edit.file, edit.before); }
      } catch { failures.push(edit.file); }
    }
    journal.status = failures.length ? 'recovery_required' : 'rolled_back';
    await fs.writeFile(path.join(recovery, 'journal.json'), JSON.stringify(journal, null, 2));
    if (failures.length) { throw new Error(`MCP setup needs recovery; inspect ${recovery}. Later edits were preserved.`); }
    throw error;
  }
  await fs.writeFile(path.join(recovery, 'journal.json'), JSON.stringify(journal, null, 2));
  return recovery;
}
