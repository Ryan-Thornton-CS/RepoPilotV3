/** Stable VS Code integration for a persistent copy/paste chat harness. */
import * as vscode from 'vscode';
import * as crypto from 'crypto';
import * as path from 'path';
import * as fs from 'fs/promises';
import { BackendClient } from './backend';
import { backendPython } from './runtime';
import { PanelMessage, panelMessage, validSourcePath, requireTrusted } from './protocol';
import { ProposalFile, ReviewDocuments } from './diffs';
import { ProjectFolder, ProjectWorkspaces } from './workspaces';

/** Chat view independent of model providers and other extensions' private interfaces. */
export class SessionPanel implements vscode.WebviewViewProvider, vscode.Disposable {
  private view?: vscode.WebviewView;
  private client?: BackendClient;
  private initializing?: Promise<BackendClient>;
  private folder?: ProjectFolder;
  private workspaces = new ProjectWorkspaces();
  private switching = false;
  private selected?: string;
  private refreshTimer?: ReturnType<typeof setInterval>;
  private refreshing = false;
  private jobs = 0;
  private epoch = 0;
  private archived = false;
  private query = '';
  private entries = new Map<string, any>();
  private opened = new Set<string>();
  private output = vscode.window.createOutputChannel('RepoPilotV3');

  /** Capture native diff storage and the installed extension's bundled resources. */
  constructor(private context: vscode.ExtensionContext, private documents: ReviewDocuments) {}

  /** Restore presentation without dispatching previous messages or copying content. */
  public resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true, localResourceRoots: [vscode.Uri.joinPath(this.context.extensionUri, 'media')] };
    view.webview.html = this.html(view.webview);
    view.webview.onDidReceiveMessage(value => {
      const message = panelMessage(value);
      if (message) { void this.handle(message).catch(error => this.error(error)); }
      else { this.error(new Error('Invalid panel message rejected.')); }
    }, undefined, this.context.subscriptions);
    view.onDidDispose(() => { this.view = undefined; });
  }

  private html(webview: vscode.Webview): string {
    const nonce = crypto.randomBytes(24).toString('base64');
    const css = webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, 'media', 'panel.css'));
    const js = webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, 'media', 'panel.js'));
    return `<!DOCTYPE html><html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
      <link rel="stylesheet" href="${css}"><title>RepoPilotV3</title></head><body>
      <header><button id="sessions-toggle" aria-label="Sessions" aria-expanded="false">☰</button><strong id="title">RepoPilotV3</strong><button data-action="new" title="New session" aria-label="New session">＋</button><button id="options-toggle" title="Options" aria-label="Options" aria-expanded="false">⚙</button></header>
      <aside id="session-drawer" hidden><input id="search" type="search" placeholder="Search sessions" aria-label="Search sessions"><label><input id="archived" type="checkbox"> Show archived</label><div id="sessions"></div></aside>
      <nav id="options" class="popover" hidden aria-label="Options">
        <button data-action="projectPrompt">Copy project prompt</button><button data-action="starter">Copy session starter</button><button data-action="handoff">Generate Handoff</button><button data-action="rename">Rename session</button><button data-action="archive">Archive / restore session</button><button data-action="delete">Delete session…</button>
        <button data-action="setup">Validation profile</button><button data-action="limit">Context threshold</button><button data-action="permission">Command permission</button><button data-action="project">External project scope</button>
        <label><input id="full" type="checkbox"> Full eligible-source package</label><button data-action="todo">Local recovery checkpoint</button><button data-action="workspace">Select workspace</button><small id="workspace-root">No project selected</small><button data-action="harness">Harness folder</button><button data-action="diagnostics">Diagnostics</button><button data-action="restart">Reconnect backend</button><button data-action="settings">Extension settings</button>
      </nav>
      <main id="transcript" aria-label="Persistent chat transcript"><button id="older">Load earlier</button><div id="entries"><p class="empty">Configure your WebUI project once with ⚙ → Copy project prompt. Start a session and paste its short starter into a new chat.</p></div><div id="pending"></div></main>
      <footer><div id="job" role="status" aria-live="polite">Local tools · no connected model</div><section class="composer">
        <textarea id="request" rows="3" placeholder="Write a message for the WebUI…" aria-label="Message or model reply"></textarea>
        <div class="composer-actions"><button id="paste" data-action="pasteRun">Paste + Run</button><button id="composer-menu" title="Composer actions" aria-label="Composer actions">⋯</button><button id="stop" data-action="stop" hidden>Stop</button><button id="send" data-action="send" class="primary">Send ↗</button></div>
        <div id="composer-options" class="popover" hidden><button id="run" data-action="run">Run composer as model reply</button><button data-action="note">Save local note</button></div>
      </section><div class="message-bar">
        <select id="mode" aria-label="Mode"><option value="plan">Planning</option><option value="ask">Asking</option><option value="code">Implementation</option><option value="review">Review</option></select>
        <button id="todo-toggle" aria-expanded="false">TODO 0/0</button><button id="context-meter" data-action="limit" title="Observed estimate against your workflow budget; remote usage is unknown">Context ~0/40k</button><button id="update-context" data-action="prepare">Update context</button>
      </div><section id="todo-panel" class="popover" hidden><strong id="task">Waiting for model task/TODO</strong><ul id="items"></ul></section>
      <section id="packages" class="popover" hidden><strong>WebUI context</strong><p id="freshness"></p><div class="toolbar"><button data-action="package">Open folder</button><button data-action="instructions">Copy instructions</button><button data-action="confirm">Confirm transfers</button></div><ul id="checklist"></ul><p>Only confirm files you actually pasted/uploaded. Local generation does not update the WebUI.</p></section>
      <div id="warning" role="status"></div></footer><script nonce="${nonce}" src="${js}"></script></body></html>`;
  }

  private post(value: unknown): void { void this.view?.webview.postMessage(value); }
  private error(error: unknown): void {
    const message = error instanceof Error ? error.message : String(error);
    this.output.appendLine(message); this.post({ type: 'error', message });
  }

  private async backend(): Promise<BackendClient> {
    if (this.initializing) { return this.initializing; }
    this.initializing = this.initializeBackend();
    try { return await this.initializing; }
    finally { this.initializing = undefined; }
  }

  private async initializeBackend(): Promise<BackendClient> {
    requireTrusted(vscode.workspace.isTrusted);
    if (!this.folder) {
      const saved = this.context.workspaceState.get<string>('workspace');
      this.folder = await this.workspaces.restore(saved, vscode.workspace.workspaceFolders);
    }
    if (!this.folder || this.folder.uri.scheme !== 'file') { throw new Error('Open a filesystem workspace first.'); }
    if (!this.client) {
      const config = vscode.workspace.getConfiguration('repopilotv3', this.folder.uri);
      const python = await backendPython(config.get<string>('pythonPath', 'bundled'), this.context.extensionUri.fsPath);
      const state = config.get<string>('statePath', '');
      if (state && !path.isAbsolute(state)) { throw new Error('statePath must be an absolute host-owned path.'); }
      const client = new BackendClient(this.folder.uri.fsPath, python, state, text => this.output.appendLine(text));
      client.on('progress', progress => {
        if (this.client !== client) { return; }
        if ((progress.method === 'command' || this.jobs) && (!progress.session_id || progress.session_id === this.selected)) { this.post({ type: 'progress', ...progress }); }
      });
      client.on('disconnected', message => { if (this.client === client) { this.post({ type: 'error', message }); } });
      this.client = client;
    }
    await this.client.connect();
    if (!this.client.hello!.initialized) {
      await this.client.request('setup', { validation: 'keep' }); this.client.hello!.initialized = true;
    }
    return this.client;
  }

  private session(): string {
    if (!this.selected) { throw new Error('Choose New session first.'); }
    return this.selected;
  }

  private async restore(): Promise<void> {
    const started = performance.now();
    this.post({ type: 'notice', message: 'Loading saved sessions…' });
    const client = await this.backend();
    this.post({ type: 'connection', workspace: client.root });
    if (!this.selected) { this.selected = (await client.request('sessions.selected')).session_id ?? undefined; }
    await this.refresh();
    this.output.appendLine(`Session view restored in ${Math.round(performance.now() - started)} ms.`);
    if (!this.refreshTimer) {
      const seconds = vscode.workspace.getConfiguration('repopilotv3', this.folder!.uri).get<number>('refreshSeconds', 5);
      this.refreshTimer = setInterval(() => void this.background(), Math.max(2, seconds) * 1000);
    }
    void this.background();
  }

  private async refresh(before?: number): Promise<void> {
    const epoch = this.epoch;
    const session = this.selected;
    const client = await this.backend();
    const sessions = await client.request('sessions.list', { ...(this.query ? { query: this.query } : {}), archived: this.archived });
    if (session !== this.selected || epoch !== this.epoch) { return; }
    if (!session) { this.post({ type: 'state', sessions, selected: null }); return; }
    let status: any, history: any[];
    try {
      [status, history] = await Promise.all([client.request('sessions.status', { session_id: session }),
        client.request('history.list', { session_id: session, ...(before ? { before } : {}) })]);
    } catch (error) {
      if (session === this.selected && epoch === this.epoch && /session_deleted:|not_found: Unknown session/.test(String(error))) {
        this.selected = undefined; this.entries.clear(); this.epoch++;
        this.post({ type: 'sessionDeleted', session });
        this.post({ type: 'state', sessions: sessions.filter((row: any) => row.id !== session), selected: null });
        return;
      }
      throw error;
    }
    if (session !== this.selected || epoch !== this.epoch) { return; }
    for (const entry of history) { this.entries.set(entry.id, entry); }
    this.post({ type: 'state', sessions, selected: session, status, history, earlier: !!before });
  }

  private async background(): Promise<void> {
    if (!this.view?.visible || !this.selected || this.refreshing || this.jobs || this.switching) { return; }
    this.refreshing = true;
    try {
      const client = await this.backend(); const session = this.selected;
      const status = await client.request('sessions.status', { session_id: session });
      if (status.session.archived || !status.session.task_id) { return; }
      if (this.switching || this.client !== client) { return; }
      const result = await client.request('exports.prepare', { session_id: session, background: true });
      if (result.deferred) { return; }
      if (!this.switching && this.client === client && session === this.selected) { await this.refresh(); }
    } catch (error) {
      if (!this.switching && /session_deleted:|not_found: Unknown session/.test(String(error))) {
        await this.refresh().catch(failure => this.output.appendLine(String(failure)));
      } else { this.output.appendLine(String(error)); }
    }
    finally { this.refreshing = false; }
  }

  private async copy(client: BackendClient, session: string | undefined, text: string,
                     notice = 'Copied. Paste into your WebUI chat.'): Promise<void> {
    if (!text) { return; }
    const started = performance.now();
    try { await vscode.env.clipboard.writeText(text); }
    catch { this.post({ type: 'notice', message: 'Clipboard unavailable. Select the message text or open saved output.' }); return; }
    const elapsed = Math.round(performance.now() - started);
    if (elapsed >= 500) { this.output.appendLine(`Slow clipboard copy: ${elapsed} ms.`); }
    if (session) { await client.request('transfers.record', { session_id: session, text }); }
    this.post({ type: 'notice', message: notice });
  }

  private async completed(client: BackendClient, session: string, result: any, epoch: number): Promise<void> {
    if (session === this.selected && epoch === this.epoch) { await this.refresh(); }
    try { await this.effects(client, session, result); }
    catch (error) { this.error(error); }
    let text = result.copy_text ?? JSON.stringify(result, null, 2);
    if (session === this.selected && epoch === this.epoch && !result.pending && text &&
        vscode.workspace.getConfiguration('repopilotv3', this.folder!.uri).get<boolean>('autoCopyResponses', true)) {
      const entryId = result.entry_id ?? (result.reply_id ? `reply-result:${result.reply_id}` : undefined);
      if (entryId) {
        try { text = (await client.request('history.copy_turn', { session_id: session, entry_id: entryId })).copy_text ?? text; }
        catch (error) { this.output.appendLine(`Full-turn copy unavailable: ${String(error)}`); }
      }
      if (session === this.selected && epoch === this.epoch) { await this.copy(client, session, text); }
    }
    if (session === this.selected) { await this.refresh(); }
    this.post({ type: 'completed', session });
  }

  private async create(client: BackendClient, epoch: number): Promise<void> {
    const status = await client.request('sessions.create');
    this.selected = status.session.id; this.entries.clear(); await this.refresh();
    const session = this.selected!;
    const result = await client.request('prompts.starter', { session_id: session });
    await this.completed(client, session, result, epoch);
  }

  private async effects(client: BackendClient, session: string, result: any): Promise<void> {
    if (session !== this.selected) { return; }
    for (const response of result.results ?? [result]) {
      const data = response.data ?? {};
      if (data.patch_id && !response.local_action && !this.opened.has(session + ':' + data.patch_id)) {
        await this.review(client, session, data.patch_id);
      }
      if (data.command_id && data.status !== 'awaiting_permission' && !this.opened.has(session + ':' + data.command_id)) {
        await this.commandDiffs(client, session, data.command_id);
      }
    }
  }

  private async review(client: BackendClient, session: string, patchId: string): Promise<void> {
    const result = await client.request('patch.review', { session_id: session, patch_id: patchId });
    for (let i = 0; i < result.files.length; i++) {
      if (session !== this.selected) { return; }
      const expanded = await client.request('patch.review', { session_id: session, patch_id: patchId, path: result.files[i].path });
      const file: ProposalFile = expanded.files[0];
      await vscode.commands.executeCommand('vscode.diff', this.documents.uri(session, patchId, file, 'original'),
        this.documents.uri(session, patchId, file, 'proposed'), `${file.path} · proposed`, { preview: false, preserveFocus: i > 0 });
    }
    this.opened.add(session + ':' + patchId);
  }

  private async commandDiffs(client: BackendClient, session: string, command: string): Promise<void> {
    const result = await client.request('commands.diffs', { session_id: session, command_id: command });
    for (let i = 0; i < result.files.length; i++) {
      if (session !== this.selected) { return; }
      const expanded = await client.request('commands.diffs', { session_id: session, command_id: command, path: result.files[i].path });
      const file: ProposalFile = expanded.files[0];
      await vscode.commands.executeCommand('vscode.diff', this.documents.uri(session, command, file, 'original'),
        this.documents.uri(session, command, file, 'proposed'), `${file.path} · observed during command`, { preview: false, preserveFocus: i > 0 });
    }
    this.opened.add(session + ':' + command);
  }

  private async saveBeforeCommand(client: BackendClient): Promise<boolean> {
    const dirty = dirtyWorkspaceEditors(client.root);
    if (!dirty.length) { return true; }
    const choice = await vscode.window.showWarningMessage('Save workspace source edits before running a command, or cancel.', { modal: true }, 'Save affected buffers');
    if (choice !== 'Save affected buffers') { return false; }
    for (const document of dirty) { if (!await document.save()) { return false; } }
    return dirtyWorkspaceEditors(client.root).length === 0;
  }

  private async importReply(client: BackendClient, session: string, text: string, epoch: number): Promise<any> {
    let hasCommands = false;
    try {
      const preview = await client.request('replies.preview', { session_id: session, text });
      hasCommands = preview.has_commands;
    } catch {
      // The importer records the actionable parser error without executing any request.
    }
    if (hasCommands && !await this.saveBeforeCommand(client)) { return; }
    const result = await client.request('replies.import', { session_id: session, text });
    await this.completed(client, session, result, epoch);
    return result;
  }

  private async resolve(client: BackendClient, session: string, replyId: string, approved: boolean, epoch: number): Promise<void> {
    const state = await client.request('replies.status', { session_id: session, reply_id: replyId });
    const pending = state.pending;
    if (!pending) { throw new Error('This reply is no longer awaiting approval. Refresh its recorded result.'); }
    if (approved && state.has_commands && !await this.saveBeforeCommand(client)) { return; }
    if (approved && pending.kind === 'patch') {
      const review = await client.request('patch.review', { session_id: session, patch_id: pending.patch_id });
      if (dirtyAffectedEditors(client.root, review.files.map((file: ProposalFile) => file.path)).length) {
        throw new Error('Affected files contain unsaved changes. Save, refresh and request a fresh proposal. Nothing was applied.');
      }
      if (!this.opened.has(session + ':' + pending.patch_id)) { await this.review(client, session, pending.patch_id); return; }
    }
    const result = await client.request('replies.resolve', { session_id: session, reply_id: replyId, approved });
    await this.completed(client, session, result, epoch);
  }

  private async source(relative: string, line = 1): Promise<void> {
    if (!this.folder || !validSourcePath(relative)) { throw new Error('Unsafe source path rejected.'); }
    const root = await fs.realpath(this.folder.uri.fsPath);
    const resolved = await fs.realpath(path.join(root, relative));
    if (path.relative(root, resolved).startsWith('..') || path.isAbsolute(path.relative(root, resolved))) { throw new Error('Source link escapes workspace.'); }
    await vscode.window.showTextDocument(vscode.Uri.file(resolved), { selection: new vscode.Range(line - 1, 0, line - 1, 0) });
  }

  private async handle(message: PanelMessage): Promise<void> {
    if (this.switching && message.action !== 'workspace') {
      if (message.action === 'draft' || message.action === 'ready') { return; }
      throw new Error('Project folder is changing. Wait for its sessions to load.');
    }
    if (message.action === 'ready') { await this.restore(); return; }
    if (message.action === 'harness') { await this.openHarness(); return; }
    if (message.action === 'workspace') { await this.chooseWorkspace(); return; }
    if (message.action === 'restart') { await this.restart(); return; }
    if (message.action === 'diagnostics') { await this.diagnostics(); return; }
    if (message.action === 'settings') { await vscode.commands.executeCommand('workbench.action.openSettings', 'repopilotv3'); return; }
    const client = await this.backend();
    if (message.action === 'draft') { await client.request('draft.save', { session_id: message.sessionId, text: message.text }); return; }
    if (message.action === 'search') { this.query = message.text!; this.archived = message.value ?? false; await this.refresh(); return; }
    if (message.action === 'history') { await this.refresh(Math.min(...[...this.entries.values()].map(e => e.sequence))); return; }
    if (message.action === 'select') { this.epoch++; this.selected = message.sessionId; this.entries.clear(); await client.request('sessions.select', { session_id: this.selected }); await this.refresh(); void this.background(); return; }
    if (message.action === 'stop') {
      const status = await client.request('sessions.status', { session_id: this.session() });
      for (const command of status.running_commands) { await client.request('commands.cancel', { session_id: this.selected, command_id: command.command_id }); }
      return;
    }
    const epoch = ++this.epoch;
    this.jobs++;
    try {
      if (message.action === 'new') { await this.create(client, epoch); return; }
      if (message.action === 'projectPrompt') {
        const session = this.selected;
        const prompt = await client.request('prompts.project');
        if (epoch === this.epoch) {
          await this.copy(client, session, prompt.text, 'Project prompt copied. Paste it into your WebUI project instructions; use the session starter for individual chats.');
        }
        return;
      }
      const session = ['archive', 'delete'].includes(message.action) ? message.sessionId ?? this.session() : this.session();
      switch (message.action) {
        case 'send': {
          if (!message.text?.trim()) { return; }
          await this.completed(client, session, await client.request('prompts.compose', { session_id: session, text: message.text }), epoch);
          this.post({ type: 'clearComposer', session, text: message.text }); break;
        }
        case 'run': {
          const result = await this.importReply(client, session, message.text!, epoch);
          if (result?.ok) { this.post({ type: 'clearComposer', session, text: message.text }); } break;
        }
        case 'pasteRun': await this.importReply(client, session, await vscode.env.clipboard.readText(), epoch); break;
        case 'resolve': await this.resolve(client, session, message.replyId!, message.value!, epoch); break;
        case 'mode': await this.completed(client, session, await client.request('sessions.configure', { session_id: session, mode: message.text }), epoch); break;
        case 'starter': await this.completed(client, session, await client.request('prompts.starter', { session_id: session }), epoch); break;
        case 'note': await client.request('notes.add', { session_id: session, text: message.text }); await this.refresh(); break;
        case 'prepare': {
          await client.request('exports.prepare', { session_id: session, ...(message.value === undefined ? {} : { full_source: message.value }) });
          await this.refresh(); this.post({ type: 'packages' }); break;
        }
        case 'package': {
          const result = await client.request('exports.prepare', { session_id: session });
          await vscode.commands.executeCommand('revealFileInOS', vscode.Uri.file(result.directory)); break;
        }
        case 'instructions': {
          const result = await client.request('exports.prepare', { session_id: session });
          await this.copy(client, session, await fs.readFile(path.join(result.directory, '00_PROJECT_INSTRUCTIONS.md'), 'utf8')); break;
        }
        case 'copy': {
          const entry = this.entries.get(message.entryId!);
          if (!entry) { throw new Error('Refresh history before copying this message.'); }
          const turn = await client.request('history.copy_turn', { session_id: session, entry_id: entry.id });
          if (session === this.selected && epoch === this.epoch) { await this.copy(client, session, turn.copy_text); }
          break;
        }
        case 'confirm': {
          const status = await client.request('sessions.status', { session_id: session });
          const picked = await vscode.window.showQuickPick(status.checklist.map((r: any) => ({ label: r.file, description: r.action, row: r })), { canPickMany: true, title: 'Confirm only completed external transfers' }) as { row: any }[] | undefined;
          if (picked?.length) { await client.request('transfers.confirm', { session_id: session, generation_id: status.generation_id, confirmations: Object.fromEntries(picked.map(p => [p.row.file, p.row.hash])) }); await this.refresh(); }
          break;
        }
        case 'handoff': {
          await client.request('handoff', { session_id: session });
          const yes = await vscode.window.showInformationMessage('Open a new external WebUI chat, then confirm to create its local continuation.', 'New external chat is open');
          if (!yes) { return; }
          const result = await client.request('handoff', { session_id: session, confirmed_new_chat: true });
          this.selected = result.session_id; this.entries.clear();
          await this.completed(client, result.session_id, result, epoch); break;
        }
        case 'review': await this.review(client, session, message.patchId!); break;
        case 'commandDiff': await this.commandDiffs(client, session, message.commandId!); break;
        case 'approve': case 'reject': {
          const review = await client.request('patch.review', { session_id: session, patch_id: message.patchId });
          if (message.action === 'approve' && dirtyAffectedEditors(client.root, review.files.map((f: ProposalFile) => f.path)).length) { throw new Error('Save affected buffers and request refreshed source before applying.'); }
          if (message.action === 'approve' && !this.opened.has(session + ':' + message.patchId)) { await this.review(client, session, message.patchId!); return; }
          await this.completed(client, session, await client.request('patch.approve', { session_id: session, patch_id: message.patchId, approved: message.action === 'approve' }), epoch); break;
        }
        case 'recover': await this.completed(client, session, await client.request('patch.recover', { session_id: session, patch_id: message.patchId }), epoch); break;
        case 'source': await this.source(message.path!, message.line); break;
        case 'rename': {
          const name = await vscode.window.showInputBox({ title: 'Session name' });
          if (name?.trim()) { await client.request('sessions.update', { session_id: session, name }); await this.refresh(); } break;
        }
        case 'archive': {
          const status = await client.request('sessions.status', { session_id: session });
          await client.request('sessions.update', { session_id: session, archived: !status.session.archived }); await this.refresh(); break;
        }
        case 'delete': {
          const status = await client.request('sessions.status', { session_id: session });
          const choice = await vscode.window.showWarningMessage(`Delete “${status.session.name || 'New session'}”?`,
            { modal: true, detail: 'This removes its chat transcript, draft and recorded conversation counts. Shared tasks, validation evidence, immutable packages and recovery files are kept. This cannot be undone.' }, 'Delete session');
          if (choice !== 'Delete session') { return; }
          this.post({ type: 'sessionDeleting', session });
          try { await client.request('sessions.delete', { session_id: session }); }
          catch (error) { this.post({ type: 'sessionDeleteFailed', session }); throw error; }
          this.post({ type: 'sessionDeleted', session });
          if (this.selected === session) {
            this.entries.clear();
            this.selected = (await client.request('sessions.list', { archived: this.archived }))[0]?.id;
            if (this.selected) { await client.request('sessions.select', { session_id: this.selected }); }
          }
          await this.refresh(); break;
        }
        case 'setup': {
          const validation = await vscode.window.showQuickPick(['keep', 'python', 'dotnet_test', 'dotnet_build'], { title: 'Approve a validation profile' });
          if (validation) { await client.request('setup', { validation }); await this.refresh(); } break;
        }
        case 'limit': {
          const status = await client.request('sessions.status', { session_id: session });
          const value = await vscode.window.showInputBox({ title: 'Observed-context workflow threshold (estimated tokens)', value: String(status.session.context_limit), validateInput: text => /^\d+$/.test(text) && +text >= 1000 && +text <= 2000000 ? undefined : 'Enter 1,000–2,000,000.' });
          if (value) { await client.request('sessions.configure', { session_id: session, context_limit: +value }); await this.refresh(); } break;
        }
        case 'permission': {
          const choice = await vscode.window.showQuickPick(['Revoke command permission', 'Allow commands for this session'], { title: 'Commands run with your account permissions and may modify files or use the network' });
          if (choice) { await client.request('sessions.configure', { session_id: session, commands_allowed: choice.startsWith('Allow') }); await this.refresh(); } break;
        }
        case 'project': {
          const status = await client.request('sessions.status', { session_id: session });
          const project = await vscode.window.showInputBox({ title: 'External project scope for confirmed uploads', value: status.session.project });
          if (project?.trim()) { await client.request('sessions.configure', { session_id: session, project }); await this.refresh(); } break;
        }
        case 'todo': {
          const text = await vscode.window.showInputBox({ title: 'Local recovery checkpoint (does not establish test evidence)' });
          if (text?.trim()) { await client.request('todo.update', { session_id: session, progress: { decisions: [text] } }); await this.refresh(); } break;
        }
      }
    } finally {
      this.jobs--;
      if (!this.jobs) { void this.background(); }
    }
  }

  /** Open authoritative static files without copying or changing workspace configuration. */
  public async openHarness(): Promise<void> {
    const choice = await vscode.window.showInformationMessage('Copy or merge the static harness manually.', 'Open folder', 'Read instructions');
    if (choice === 'Open folder') { await vscode.commands.executeCommand('revealFileInOS', vscode.Uri.joinPath(this.context.extensionUri, 'harness', 'workspace')); }
    else if (choice) { await vscode.window.showTextDocument(vscode.Uri.joinPath(this.context.extensionUri, 'harness', 'README.md')); }
  }

  /** Select an individual project folder and restore only that folder's private sessions. */
  public async chooseWorkspace(): Promise<void> {
    requireTrusted(vscode.workspace.isTrusted);
    if (this.jobs || this.switching) { throw new Error('Wait for current tool work before changing project folders.'); }
    this.switching = true;
    try {
      const folder = await this.workspaces.choose(this.folder); if (!folder) { return; }
      if (this.jobs) { throw new Error('Wait for current tool work before changing project folders.'); }
      if (folder.uri.toString() === this.folder?.uri.toString()) { return; }
      if (this.initializing) { await this.initializing; }
      this.epoch++;
      await this.client?.close();
      if (this.refreshTimer) { clearInterval(this.refreshTimer); this.refreshTimer = undefined; }
      this.client = undefined; this.folder = folder; this.selected = undefined;
      this.entries.clear(); this.opened.clear();
      await this.context.workspaceState.update('workspace', folder.uri.toString());
      this.post({ type: 'workspaceChanged', workspace: folder.uri.fsPath });
      await this.restore();
    } finally { this.switching = false; }
  }

  /** Reconnect and read passive history without replaying accepted requests. */
  public async restart(): Promise<void> {
    if (this.jobs || this.refreshing || this.switching) { throw new Error('Stop current commands and wait for work before reconnecting.'); }
    await this.client?.close(); this.client = undefined; await this.restore();
  }

  /** Show local environment diagnostics in a data-only editor document. */
  public async diagnostics(): Promise<void> {
    const data = await (await this.backend()).request('diagnostics');
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({ content: JSON.stringify(data, null, 2), language: 'json' }));
  }

  /** Release presentation resources and disconnect the backend supervisor pipes. */
  public dispose(): void {
    if (this.refreshTimer) { clearInterval(this.refreshTimer); }
    this.client?.dispose(); this.output.dispose();
  }
}

/** Register only stable extension APIs available in VS Code 1.85 and later. */
export function activate(context: vscode.ExtensionContext): void {
  const documents = new ReviewDocuments(); const panel = new SessionPanel(context, documents);
  context.subscriptions.push(panel, vscode.workspace.registerTextDocumentContentProvider('repopilotv3-review', documents),
    vscode.window.registerWebviewViewProvider('repopilotv3.sessions', panel),
    vscode.commands.registerCommand('repopilotv3.open', () => vscode.commands.executeCommand('repopilotv3.sessions.focus')),
    vscode.commands.registerCommand('repopilotv3.openHarness', () => panel.openHarness()),
    vscode.commands.registerCommand('repopilotv3.selectWorkspace', () => panel.chooseWorkspace()),
    vscode.commands.registerCommand('repopilotv3.restart', () => panel.restart()),
    vscode.commands.registerCommand('repopilotv3.diagnostics', () => panel.diagnostics()));
}

/** Return dirty file buffers belonging to this workspace, excluding private/escaping paths. */
export function dirtyWorkspaceEditors(root: string): vscode.TextDocument[] {
  return vscode.workspace.textDocuments.filter(document => document.uri.scheme === 'file' && document.isDirty &&
    validSourcePath(path.relative(root, document.uri.fsPath).split(path.sep).join('/')));
}

/** Detect affected unsaved buffers again immediately before patch approval. */
export function dirtyAffectedEditors(root: string, paths: string[]): vscode.TextDocument[] {
  const normalize = (value: string): string => process.platform === 'win32' ? path.resolve(value).toLowerCase() : path.resolve(value);
  const affected = new Set(paths.map(p => normalize(path.join(root, p))));
  return vscode.workspace.textDocuments.filter(d => d.uri.scheme === 'file' && d.isDirty && affected.has(normalize(d.uri.fsPath)));
}
