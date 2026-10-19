/** Closed, data-only messages allowed from the webview into the extension host. */
export interface PanelMessage {
  action: string;
  text?: string;
  sessionId?: string;
  entryId?: string;
  patchId?: string;
  path?: string;
  line?: number;
  value?: boolean;
  replyId?: string;
  commandId?: string;
}

const actions = new Set(['send', 'resolve', 'mode', 'starter', 'projectPrompt', 'limit', 'permission', 'project', 'commandDiff',
  'stop', 'diagnostics', 'settings', 'ready', 'setup', 'new', 'select', 'rename', 'archive', 'delete', 'search', 'run',
  'pasteRun', 'note', 'draft', 'copy', 'prepare', 'instructions', 'package', 'confirm', 'handoff',
  'review', 'approve', 'reject', 'recover', 'source', 'todo', 'history', 'workspace', 'harness', 'restart']);

/** Reject unknown actions, extra properties, oversized data and malformed values. */
export function panelMessage(value: unknown): PanelMessage | undefined {
  if (!value || typeof value !== 'object' || Array.isArray(value)) { return; }
  const p = value as Record<string, unknown>;
  if (typeof p.action !== 'string' || !actions.has(p.action)) { return; }
  if (Object.keys(p).some(k => !['action', 'text', 'sessionId', 'entryId', 'patchId', 'path', 'line', 'value', 'replyId', 'commandId'].includes(k))) { return; }
  for (const key of ['text', 'sessionId', 'entryId', 'patchId', 'path', 'replyId', 'commandId']) {
    if (p[key] !== undefined && (typeof p[key] !== 'string' || (p[key] as string).length > (key === 'text' ? 2500000 : 1000))) { return; }
  }
  if (p.line !== undefined && (!Number.isInteger(p.line) || (p.line as number) < 1 || (p.line as number) > 10000000)) { return; }
  if (p.value !== undefined && typeof p.value !== 'boolean') { return; }
  if (['select', 'draft'].includes(p.action) && typeof p.sessionId !== 'string') { return; }
  if (['send', 'mode', 'run', 'note', 'draft', 'search'].includes(p.action) && typeof p.text !== 'string') { return; }
  if (p.action === 'mode' && !['plan', 'ask', 'code', 'review'].includes(String(p.text))) { return; }
  if (p.action === 'resolve' && (typeof p.replyId !== 'string' || typeof p.value !== 'boolean')) { return; }
  if (p.action === 'commandDiff' && typeof p.commandId !== 'string') { return; }
  if (['review', 'approve', 'reject', 'recover'].includes(p.action) && typeof p.patchId !== 'string') { return; }
  if (p.action === 'source' && typeof p.path !== 'string') { return; }
  if (p.action === 'copy' && typeof p.entryId !== 'string') { return; }
  return p as unknown as PanelMessage;
}

/** Resolve only repository-relative source paths; no URI or traversal is accepted. */
export function validSourcePath(value: string): boolean {
  return !!value && !value.includes('\\') && !value.includes(':') && !value.includes('\0') &&
    !value.startsWith('/') && !value.split('/').some(p => p.endsWith('.') || p.endsWith(' ') ||
      /^(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\.|$)/i.test(p) ||
      ['..', '.git', '.repopilot', '.repopilotv2', '.repopilotv3'].includes(p.toLowerCase()));
}

/** Enforce trust before environment detection or any local backend process is created. */
export function requireTrusted(trusted: boolean): void {
  if (!trusted) { throw new Error('Trust this workspace before starting the local Python backend.'); }
}
