/** Native filesystem identities shared by transport, MCP and editor-buffer checks. */
import * as fs from 'fs';
import * as path from 'path';

/** Resolve existing ancestors natively, including Windows 8.3 names and symlink aliases. */
export function canonicalPath(value: string): string {
  const absolute = path.resolve(value);
  let current = absolute;
  const suffix: string[] = [];
  while (true) {
    try { return path.join(fs.realpathSync.native(current), ...suffix.reverse()); }
    catch (error) {
      if (!['ENOENT', 'ENOTDIR'].includes((error as NodeJS.ErrnoException).code ?? '') || path.dirname(current) === current) { throw error; }
      suffix.push(path.basename(current)); current = path.dirname(current);
    }
  }
}

/** Normalize only OS-equivalent identities; never confuse separate repository directories. */
export function pathIdentity(value: string): string {
  const canonical = canonicalPath(value);
  if (process.platform !== 'win32') { return canonical; }
  return canonical.replace(/^\\\\\?\\UNC\\/i, '\\\\').replace(/^\\\\\?\\/, '').toLowerCase();
}

/** Compare physical repository/configuration identities without requiring string-spelling equality. */
export function samePath(left: string, right: string): boolean {
  return pathIdentity(left) === pathIdentity(right);
}
