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
import { McpIntegration, repositoryServerName } from './mcp';
import { canonicalPath, pathIdentity } from './paths';
import { ProjectRegistry } from './projects';
import { ConfigurationEdit } from './configuration';

/** Chat view independent of model providers and other extensions' private interfaces. */
export class SessionPanel implements vscode.WebviewViewProvider, vscode.Disposable {
  private view?: vscode.WebviewView;
  private client?: BackendClient;
  private initializing?: Promise<BackendClient>;
  private folder?: ProjectFolder;
  private workspaces = new ProjectWorkspaces();
  private switching = false;
  private projects: ProjectRegistry;
  private integration?: McpIntegration;
  private selected?: string;
  private refreshTimer?: ReturnType<typeof setInterval>;
  private refreshing = false;
  private jobs = 0;
  private continuing = new Set<string>();
  private packageStates = new Map<string, { state: string; error?: string }>();
  private epoch = 0;
  private archived = false;
  private query = '';
  private entries = new Map<string, any>();
  private opened = new Set<string>();
  private latestDrafts = new Map<string, string>();
  private output = vscode.window.createOutputChannel('RepoPilotV3');

  /** Capture native diff storage and the installed extension's bundled resources. */
  constructor(private context: vscode.ExtensionContext, private documents: ReviewDocuments) { this.projects = new ProjectRegistry(context.workspaceState); }

  /** Restore presentation without dispatching previous messages or copying content. */
  public resolveWebviewView(view: vscode.WebviewView): void {
    this.view = view;
    view.webview.options = { enableScripts: true, localResourceRoots: [vscode.Uri.joinPath(this.context.extensionUri, 'media'), vscode.Uri.joinPath(this.context.extensionUri, 'docs')] };
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
      <meta http-equiv="Content-Security-Policy" content="default-src 'none'; img-src ${webview.cspSource}; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
      <link rel="stylesheet" href="${css}"><title>RepoPilotV3</title></head><body>
      <nav id="top-tabs" role="tablist" aria-label="RepoPilot pages"><button id="projects-tab" role="tab" aria-selected="true" aria-controls="projects-page">Projects</button><button id="documentation-tab" role="tab" aria-selected="false" aria-controls="documentation-page">Documentation</button></nav>
      <section id="documentation-page" role="tabpanel" aria-labelledby="documentation-tab" hidden><div class="search-header"><input id="help-search" type="search" placeholder="Search documentation" aria-label="Search documentation"></div><div id="help-topics"></div></section>
      <section id="projects-page" role="tabpanel" aria-labelledby="projects-tab">
      <div class="project-bar"><select id="project-select" aria-label="Project space"><option value="">Select project</option></select><button data-action="projectAdd" title="Add a project folder">＋ Project</button><button id="project-menu-toggle" aria-label="Project actions" aria-expanded="false" aria-controls="project-menu">⋯</button><nav id="project-menu" class="popover" hidden aria-label="Project actions"><button data-action="projectRename">Rename project</button><button data-action="projectRemove">Remove project…</button></nav></div>
      <nav id="project-tabs" role="tablist" aria-label="Project pages"><button id="sessions-tab" role="tab" aria-selected="true" aria-controls="session-page">Sessions</button><button id="context-tab" role="tab" aria-selected="false" aria-controls="context-page">Context files</button></nav>
      <section id="context-page" role="tabpanel" aria-labelledby="context-tab" hidden><h2>WebUI context files</h2><p id="context-freshness">Select a session to view its package.</p><div class="toolbar"><button data-action="prepare">Retry refresh</button><button data-action="package">Open folder</button><button data-action="confirm">Confirm transfers</button></div><div id="context-files"></div></section>
      <section id="session-page" role="tabpanel" aria-labelledby="sessions-tab">
      <header><button id="sessions-toggle" aria-label="Sessions" aria-expanded="false" aria-controls="session-drawer">☰</button><strong id="title">RepoPilotV3</strong><button data-action="new" title="New session" aria-label="New session">＋</button><div id="options-anchor"><button id="options-toggle" title="Options" aria-label="Options" aria-expanded="false" aria-controls="options">⚙</button>      <nav id="options" class="popover" hidden aria-label="Options">
        <details><summary>Copilot &amp; MCP</summary><button data-action="mcpEnable">Enable MCP for selected project</button><button data-action="mcpStatus">MCP status</button><button data-action="mcpConfig">Open MCP configuration</button><button data-action="mcpDisable">Disable a project MCP</button><button data-action="generateHarness">Generate / update workspace harness</button><button data-action="harnessWorkspace">Open workspace .github</button><button data-action="harness">Open bundled harness</button><button data-action="harnessRecover">Recover harness update</button></details>
        <details><summary>Projects</summary><button data-action="projectAdd">Add project folder</button><button data-action="projectRename">Rename project space</button><button data-action="projectRemove">Remove project space…</button><small id="workspace-root">No project selected</small></details>
        <details><summary>Maintenance</summary><button data-action="retention">Export retention settings</button><button data-action="diagnostics">Diagnostics</button><button data-action="restart">Reconnect backend</button><button data-action="settings">Extension settings</button></details>
      </nav></div></header>
      <aside id="session-drawer" hidden><div class="search-header"><input id="search" type="search" placeholder="Search sessions" aria-label="Search sessions"><label><input id="archived" type="checkbox"> Show archived</label></div><div id="sessions"></div></aside>

      <main id="transcript" aria-label="Persistent chat transcript"><button id="older">Load earlier</button><div id="entries"><p class="empty">Configure your WebUI project once with Prompts → Copy project prompt. Start a session and paste its short starter into a new chat.</p></div><div id="pending"></div></main>
      <footer><div id="job" role="status" aria-live="polite">Local tools · no connected model</div><section class="composer">
        <textarea id="request" rows="3" placeholder="Paste model tool requests or write a message…" aria-label="Message or model reply"></textarea>
        <div class="composer-actions"><button id="paste" data-action="pasteRun">Paste + Run</button><button id="composer-menu" title="Composer actions" aria-label="Composer actions" aria-expanded="false" aria-controls="composer-options">⋯</button><button id="stop" data-action="stop" hidden>Stop</button><button id="send" data-action="send" class="primary">Send ↗</button></div>
        <div id="composer-options" class="popover" hidden><button id="run" data-action="run">Run as model reply</button><button data-action="forward">Copy message for WebUI</button><button data-action="note">Save local note</button></div>
      </section><div class="message-bar">
        <button id="chat-toggle" aria-expanded="false" aria-controls="chat-menu">Chat</button><button id="prompts-toggle" aria-expanded="false" aria-controls="prompts-menu">Prompts</button><button id="execution-toggle" aria-expanded="false" aria-controls="execution-menu">Execution</button><button id="todo-toggle" aria-expanded="false" aria-controls="todo-panel">TODO 0/0</button><button id="context-meter" data-action="limit" aria-label="Context window: estimated usage" title="Estimated recorded context"><svg viewBox="0 0 24 24" width="24" height="24" aria-hidden="true" focusable="false"><circle class="context-track" cx="12" cy="12" r="9"/><circle id="context-progress" cx="12" cy="12" r="9" pathLength="100" stroke-dasharray="100" stroke-dashoffset="100"/></svg><span id="context-description" class="sr-only">Context window: 0% full</span></button><button id="update-context" aria-controls="packages" aria-expanded="false">Context files</button>
      </div><section id="todo-panel" class="popover" hidden><div class="toolbar"><button data-action="todoReset">Reset checklist…</button></div><strong id="task">Waiting for model task/TODO</strong><ul id="items"></ul></section>
      <section id="packages" class="popover" hidden><strong>WebUI context</strong><p id="freshness" role="status"></p><div class="toolbar"><button data-action="package">Open folder</button><button data-action="instructions">Copy instructions</button><button data-action="prepare" id="retry-context" hidden>Retry refresh</button><button data-action="confirm">Confirm transfers</button></div><details><summary>Package settings</summary><button data-action="project">External project scope</button><label><input id="full" type="checkbox"> Full eligible-source package</label></details><ul id="checklist"></ul><p>Only confirm files you actually pasted/uploaded. Local generation does not update the WebUI.</p></section>
      <nav id="chat-menu" class="popover bottom-menu" hidden aria-label="Chat actions"><button data-action="starter">Copy session starter</button><button data-action="handoff">Generate handoff</button><button data-action="rename">Rename session</button><button data-action="archive">Archive / restore</button><button data-action="delete">Delete session…</button></nav>
      <nav id="prompts-menu" class="popover bottom-menu" hidden aria-label="Prompts"><button data-action="projectPrompt">Copy project prompt</button><button data-action="roleCopy" data-role="planning">Copy Planning prompt</button><button data-action="roleCopy" data-role="asking">Copy Asking prompt</button><button data-action="roleCopy" data-role="implementation">Copy Implementation prompt</button><button data-action="roleCopy" data-role="review">Copy Review prompt</button><button data-action="roleEdit">Edit prompts</button><button data-action="roleReset">Reset prompt to default…</button></nav>
      <nav id="execution-menu" class="popover bottom-menu" hidden aria-label="Execution"><button data-action="setup">Validation profile</button><button data-action="permission">Command permission</button><button data-action="todo">Recovery checkpoint</button></nav>
      <div class="continuation-bar"><div id="warning" role="status"></div><button id="continue-chat" data-action="handoff" hidden>Continue in new chat</button></div></footer></section></section><script nonce="${nonce}" src="${js}"></script></body></html>`;
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
      if (!saved && this.context.workspaceState.get<boolean>('project.registryInitialized')) { throw new Error('Select or add a project folder first.'); }
      this.folder = await this.workspaces.restore(saved, vscode.workspace.workspaceFolders);
    }
    if (this.folder) {
      this.folder.uri = vscode.Uri.file(await fs.realpath(this.folder.uri.fsPath));
      const project = await this.projects.add(this.folder.uri.toString(), this.folder.name); this.folder.name = project.name;
      await this.context.workspaceState.update('workspace', this.folder.uri.toString());
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
    const legacy = this.context.workspaceState.get<string>('workspace');
    if (legacy && !this.projects.list().some(p => p.uri === legacy)) { await this.projects.add(legacy, path.basename(vscode.Uri.parse(legacy).fsPath)); }
    await this.projectState();
    await this.documentation();
    if (!this.folder && !this.context.workspaceState.get<string>('workspace') && (this.context.workspaceState.get<boolean>('project.registryInitialized') || !vscode.workspace.workspaceFolders?.length)) { this.post({ type: 'state', sessions: [], selected: null }); return; }
    const client = await this.backend();
    await this.projectState();
    this.post({ type: 'connection', workspace: client.root });
    if (!this.selected) { this.selected = (await client.request('sessions.selected')).session_id ?? undefined; }
    await this.refresh();
    const harness = client.hello?.methods?.includes('harness.status') ? await client.request('harness.status') : { stale_runtime: false, recovery_required: [] };
    if (harness.stale_runtime) { this.post({ type: 'notice', message: 'Workspace harness uses an older runtime. Use Copilot & MCP → Generate / update workspace harness to preview updates.' }); }
    if (harness.recovery_required.length) { this.post({ type: 'notice', message: 'An interrupted harness update needs recovery. Use Copilot & MCP → Recover harness update.' }); }
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
    this.post({ type: 'state', sessions, selected: session, status, history, earlier: !!before, contextRefresh: this.packageStates.get(session) });
  }

  private async background(): Promise<void> {
    if (!this.view?.visible || !this.selected || this.refreshing || this.jobs || this.switching) { return; }
    this.refreshing = true;
    const session = this.selected;
    try {
      const client = await this.backend();
      const status = await client.request('sessions.status', { session_id: session });
      if (status.session.archived) { return; }
      if (this.switching || this.client !== client) { return; }
      this.packageState(session, 'refreshing');
      const result = await client.request('exports.prepare', { session_id: session, background: true });
      if (result.deferred) { this.packageState(session, 'queued'); return; }
      this.packageState(session, 'current');
      if (!this.switching && this.client === client && session === this.selected) { await this.refresh(); }
    } catch (error) {
      if (!this.switching && /session_deleted:|not_found: Unknown session/.test(String(error))) {
        await this.refresh().catch(failure => this.output.appendLine(String(failure)));
      } else {
        if (!this.switching && session === this.selected) this.packageState(session, 'failed', String(error));
      }
    }
    finally { this.refreshing = false; }
  }

  /** Publish lightweight refresh status without changing history, clipboard or transfer receipts. */
  private packageState(session: string, state: string, error?: string): void {
    const previous = this.packageStates.get(session);
    this.packageStates.set(session, { state, ...(error ? { error } : {}) });
    if (error && previous?.error !== error) this.output.appendLine(error);
    if (session === this.selected) this.post({ type: 'contextRefresh', session, state, error });
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
    if (message.action === 'draft' && message.sessionId && message.text !== undefined) { this.latestDrafts.set(message.sessionId, message.text); }
    if (this.switching && !['workspace', 'projectAdd', 'projectSelect'].includes(message.action)) {
      if (message.action === 'draft' || message.action === 'ready') { return; }
      throw new Error('Project folder is changing. Wait for its sessions to load.');
    }
    if (message.action.startsWith('project') && ['projectAdd', 'projectSelect', 'projectRename', 'projectRemove'].includes(message.action)) { await this.projectAction(message); return; }
    if (['roleCopy', 'roleEdit', 'roleReset'].includes(message.action)) { await this.roleAction(message); return; }
    if (message.action === 'ready') { await this.restore(); return; }
    if (['mcpEnable', 'mcpStatus', 'mcpConfig', 'mcpDisable', 'generateHarness', 'harnessWorkspace', 'harnessRecover', 'retention'].includes(message.action)) { await this.integrationAction(message.action); return; }
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
    if (message.action === 'handoff' && this.selected && this.continuing.has(this.selected)) return;
    if (message.action === 'handoff' && this.jobs) throw new Error('Finish or stop current work before creating a continuation.');
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
          const preview = await client.request('composer.preview', { session_id: session, text: message.text });
          if (preview.has_commands && !await this.saveBeforeCommand(client)) { return; }
          const result = await client.request('composer.submit', { session_id: session, text: message.text });
          await this.completed(client, session, result, epoch);
          if (result.ok !== false) { this.post({ type: 'clearComposer', session, text: message.text }); }
          break;
        }
        case 'forward': {
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
        case 'starter': await this.completed(client, session, await client.request('prompts.starter', { session_id: session }), epoch); break;
        case 'note': await client.request('notes.add', { session_id: session, text: message.text }); await this.refresh(); break;
        case 'prepare': {
          this.packageState(session, 'refreshing');
          try {
            await client.request('exports.prepare', { session_id: session, ...(message.value === undefined ? {} : { full_source: message.value }) });
            this.packageState(session, 'current');
          } catch (error) { this.packageState(session, 'failed', String(error)); throw error; }
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
          if (this.continuing.has(session)) return;
          this.continuing.add(session);
          this.post({ type: 'continuing', session, value: true });
          try {
            await client.request('handoff', { session_id: session });
            const yes = await vscode.window.showInformationMessage('Open a new external WebUI chat, then confirm to create its local continuation.', 'New external chat is open');
            if (!yes || this.selected !== session) return;
            const result = await client.request('handoff', { session_id: session, confirmed_new_chat: true });
            this.selected = result.session_id; this.entries.clear();
            await this.completed(client, result.session_id, result, this.epoch);
          } finally { this.continuing.delete(session); this.post({ type: 'continuing', session, value: false }); }
          break;
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
        case 'contextFile': {
          const status = await client.request('sessions.status', { session_id: session });
          if (!status.directory || !status.checklist.some((row: any) => row.file === message.path)) { throw new Error('Refresh context before opening this file.'); }
          const directory = await fs.realpath(status.directory);
          const file = await fs.realpath(path.join(directory, message.path!));
          const relative = path.relative(directory, file);
          if (relative.startsWith('..') || path.isAbsolute(relative)) { throw new Error('Context path escapes its published package.'); }
          await vscode.window.showTextDocument(vscode.Uri.file(file)); break;
        }
        case 'todoReset': {
          const status = await client.request('sessions.status', { session_id: session });
          if (!status.task) { throw new Error('Create a task before resetting its checklist.'); }
          const choice = await vscode.window.showWarningMessage('Reset this task’s checklist?', { modal: true,
            detail: 'This also resets checklist readiness in continuation chats sharing this task. Previous items are archived; objectives, requirements and validation evidence remain.' }, 'Reset checklist');
          if (choice === 'Reset checklist') { await this.completed(client, session, await client.request('todo.reset', { session_id: session, expected_revision: status.task.revision }), epoch); } break;
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

  /** Register the optional stable MCP API without raising the panel's minimum editor version. */
  public registerMcp(): void {
    this.integration = new McpIntegration(this.context, edits => this.previewConfiguration(edits));
    this.context.subscriptions.push(this.integration);
  }

  /** Show exact configuration diffs before any native-provider migration or fallback write. */
  private async previewConfiguration(edits: ConfigurationEdit[]): Promise<boolean> {
    for (const edit of edits) {
      const file = { path: path.basename(edit.file), original: edit.before ?? '', proposed: edit.after, status: 'configuration' };
      const key = crypto.createHash('sha256').update(edit.file + edit.after).digest('hex').slice(0, 16);
      await vscode.commands.executeCommand('vscode.diff', this.documents.uri('configuration', key, file, 'original'),
        this.documents.uri('configuration', key, file, 'proposed'), `${edit.file} · MCP setup`, { preview: false });
    }
    return await vscode.window.showWarningMessage(`Apply ${edits.length} previewed MCP configuration change(s)? Unrelated entries are preserved.`, { modal: true }, 'Apply configuration') === 'Apply configuration';
  }

  /** Perform explicit integration setup without requiring or creating a manual chat session. */
  public async integrationAction(action: string): Promise<void> {
    requireTrusted(vscode.workspace.isTrusted);
    if (this.jobs || this.switching) { throw new Error('Wait for current tool work before changing integrations.'); }
    this.jobs++;
    try {
      const client = await this.backend();
      const root = await fs.realpath(client.root);
      const state = vscode.workspace.getConfiguration('repopilotv3', vscode.Uri.file(root)).get<string>('statePath', '');
      if (!this.integration) { this.registerMcp(); }
      const mcp = this.integration!;
      if (action === 'mcpEnable') { await mcp.enable(root, state); return; }
      if (action === 'mcpDisable') { await mcp.disable(); return; }
      if (action === 'mcpStatus') { await mcp.status(root); return; }
      if (action === 'mcpConfig') { await mcp.configuration(root, state); return; }
      if (action === 'harnessWorkspace') {
        const uri = vscode.Uri.file(path.join(root, '.github'));
        try { await fs.access(uri.fsPath); } catch { throw new Error('This project has no .github folder yet. Use Generate / update workspace harness.'); }
        await vscode.commands.executeCommand('revealFileInOS', uri); return;
      }
      if (action === 'retention') {
        await vscode.window.showTextDocument(vscode.Uri.file(path.join(state || path.join(root, '.repopilotv3'), 'config.json'))); return;
      }
      const targets = [root];
      const enclosing = vscode.workspace.getWorkspaceFolder(vscode.Uri.file(root))?.uri.fsPath;
      if (enclosing && enclosing !== root) { targets.push(await fs.realpath(enclosing)); }
      const target = targets.length === 1 ? root : await vscode.window.showQuickPick(targets, { title: 'Workspace harness target (selected project is first)' });
      if (!target) { return; }
      let targetClient = client;
      try {
        if (target !== root) {
          targetClient = new BackendClient(target, await backendPython('bundled', this.context.extensionUri.fsPath), '', text => this.output.appendLine(text));
          await targetClient.connect(); await targetClient.request('setup', { validation: 'keep' });
        }
        if (!targetClient.hello?.methods?.includes('harness.status')) { throw new Error('Your custom Python backend does not support harness setup. Select bundled Python or update the custom environment.'); }
        const status = await targetClient.request('harness.status');
        if (action === 'harnessRecover') {
          const selected = await vscode.window.showQuickPick(status.recovery_required, { title: 'Recover an interrupted harness update' });
          if (selected) { await targetClient.request('harness.recover', { recovery_id: selected }); void vscode.window.showInformationMessage('Harness update restored.'); } return;
        }
        const existing = await mcp.existing(root);
        let registration = mcp.registrations().find(row => row.root === root);
        if (!registration && (!existing.length || existing.every(entry => entry.keys[0] === 'mcpServers'))) {
          const choice = await vscode.window.showQuickPick(['Enable MCP and generate harness', 'Generate harness only'], { title: 'Copilot workspace setup' });
          if (!choice) { return; }
          if (choice === 'Enable MCP and generate harness') { await mcp.enable(root, state); registration = mcp.registrations().find(row => row.root === root); if (!registration) { return; } }
        }
        const name = registration?.label ?? existing[0]?.name ?? repositoryServerName(root);
        const result = await targetClient.request('harness.preview', { repository_root: root, ...(state ? { repository_state: state } : {}), server_name: name });
        for (const file of result.files.filter((row: any) => row.action !== 'keep')) {
          await vscode.commands.executeCommand('vscode.diff', this.documents.uri('harness', result.plan_id, file, 'original'),
            this.documents.uri('harness', result.plan_id, file, 'proposed'), `${file.path} · harness ${file.action}`, { preview: false });
        }
        if (result.conflicts.length) { throw new Error(`Managed-file conflicts: ${result.conflicts.join(', ')}. Inspect the diffs; no harness files were changed.`); }
        if (status.stale_runtime) { this.output.appendLine('Harness runtime/version is stale; the preview updates it explicitly.'); }
        if (await vscode.window.showInformationMessage(`Generate/update the previewed Copilot harness in ${target}?`, { modal: true }, 'Apply harness') !== 'Apply harness') { return; }
        if (dirtyAffectedEditors(target, result.files.map((file: any) => file.path)).length) { throw new Error('Save affected harness files and create a fresh preview first.'); }
        requireTrusted(vscode.workspace.isTrusted);
        const written = await targetClient.request('harness.apply', { plan_id: result.plan_id });
        void vscode.window.showInformationMessage(`Harness ready: ${written.directory}. ${written.changed_files.length} file(s) changed.`);
      } finally { if (targetClient !== client) { await targetClient.close(); } }
    } finally { this.jobs--; void this.background(); }
  }

  /** Publish project metadata independently of session creation and repository indexing. */
  private async projectState(): Promise<void> {
    this.post({ type: 'projects', projects: this.projects.list(), selectedUri: this.folder?.uri.toString() });
  }

  /** Load shipped help as data, with image paths restricted to bundled documentation. */
  private async documentation(): Promise<void> {
    const topics = JSON.parse(await fs.readFile(path.join(this.context.extensionUri.fsPath, 'docs/help.json'), 'utf8'));
    for (const topic of topics) {
      if (typeof topic.image === 'string' && /^images\/[a-z0-9-]+\.png$/.test(topic.image)) {
        topic.image = this.view?.webview.asWebviewUri(vscode.Uri.joinPath(this.context.extensionUri, 'docs', topic.image)).toString();
      } else { delete topic.image; }
    }
    this.post({ type: 'documentation', topics });
  }

  /** Handle folder-space metadata without deleting source, state or MCP registrations. */
  private async projectAction(message: PanelMessage): Promise<void> {
    requireTrusted(vscode.workspace.isTrusted);
    if (this.jobs || this.switching) { throw new Error('Finish or explicitly stop current work before changing project spaces.'); }
    if (message.action === 'projectAdd') { await this.chooseWorkspace(); return; }
    const project = message.projectId ? this.projects.get(message.projectId) : this.projects.list().find(p => p.uri === this.folder?.uri.toString());
    if (!project) { throw new Error('Select a project first.'); }
    if (message.action === 'projectSelect') {
      try { await this.switchProject(await this.workspaces.resolve(vscode.Uri.parse(project.uri))); }
      finally { await this.projectState(); }
      return;
    }
    if (message.action === 'projectRename') {
      const name = await vscode.window.showInputBox({ title: 'Project space name', value: project.name });
      if (name) { await this.projects.rename(project.id, name); if (this.folder?.uri.toString() === project.uri) { this.folder.name = name; } }
    } else if (message.action === 'projectRemove') {
      if (await vscode.window.showWarningMessage(`Remove “${project.name}” from RepoPilot?`, { modal: true, detail: 'The folder, private state, sessions and MCP registration are kept.' }, 'Remove project') !== 'Remove project') { return; }
      if (this.jobs || this.switching) throw new Error('Finish or stop current work before removing a project.');
      this.switching = true;
      try {
        if (project.uri === this.folder?.uri.toString()) {
          this.epoch++;
          await this.client?.close();
          // Retain the listing if shutdown fails; never reimport a removed space on restore.
          await this.context.workspaceState.update('workspace', undefined);
          this.client = undefined; this.folder = undefined; this.selected = undefined;
          this.entries.clear(); this.opened.clear(); this.packageStates.clear();
          if (this.refreshTimer) { clearInterval(this.refreshTimer); this.refreshTimer = undefined; }
          this.post({ type: 'workspaceChanged', workspace: '' });
        }
        await this.projects.remove(project.id);
      } finally { this.switching = false; }
    }
    await this.projectState();
  }

  /** Browse a canonical folder and retain its previous space when switching. */
  public async chooseWorkspace(): Promise<void> {
    requireTrusted(vscode.workspace.isTrusted);
    if (this.jobs || this.switching) { throw new Error('Finish or explicitly stop current work before changing projects.'); }
    const folder = await this.workspaces.choose(this.folder);
    if (folder) { await this.switchProject(folder); }
  }

  /** Change backend ownership only after pending work finishes; restoration never replays. */
  private async switchProject(folder: ProjectFolder): Promise<void> {
    if (this.jobs || this.switching) { throw new Error('Finish or explicitly stop current work before changing projects.'); }
    if (this.client?.hello && this.selected) {
      const status = await this.client.request('sessions.status', { session_id: this.selected });
      if (status.running_commands?.length) { throw new Error('Stop the running command before changing projects.'); }
    }
    folder.uri = vscode.Uri.file(await fs.realpath(folder.uri.fsPath));
    const project = await this.projects.add(folder.uri.toString(), folder.name); folder.name = project.name;
    if (folder.uri.toString() === this.folder?.uri.toString()) { this.folder = folder; await this.projectState(); return; }
    this.switching = true;
    try {
      if (this.initializing) { await this.initializing; }
      this.epoch++;
      if (this.client && this.selected && this.latestDrafts.has(this.selected)) { await this.client.request('draft.save', { session_id: this.selected, text: this.latestDrafts.get(this.selected) }); }
      await this.client?.close();
      if (this.refreshTimer) { clearInterval(this.refreshTimer); this.refreshTimer = undefined; }
      this.client = undefined; this.folder = folder; this.selected = undefined;
      this.entries.clear(); this.opened.clear(); this.packageStates.clear();
      await this.context.workspaceState.update('workspace', folder.uri.toString());
      this.post({ type: 'workspaceChanged', workspace: folder.uri.fsPath });
      await this.restore();
    } finally { this.switching = false; }
  }

  /** Copy exact project-authored guidance or explicitly edit/reset a selected prompt file. */
  private async roleAction(message: PanelMessage): Promise<void> {
    requireTrusted(vscode.workspace.isTrusted);
    if (this.switching || this.jobs) { throw new Error('Wait for current work before editing or copying prompts.'); }
    this.jobs++;
    try {
      const role = message.text ?? await vscode.window.showQuickPick(['planning', 'asking', 'implementation', 'review'], { title: 'Role prompt' });
      if (!role) { return; }
      const client = await this.backend();
      const prompt = await client.request('prompts.read', { role });
      if (message.action === 'roleEdit') { await vscode.window.showTextDocument(vscode.Uri.file(prompt.path)); return; }
      if (message.action === 'roleReset') {
        if (vscode.workspace.textDocuments.some(d => d.uri.fsPath === prompt.path && d.isDirty)) { throw new Error('Save or discard the prompt’s unsaved edits before resetting.'); }
        if (await vscode.window.showWarningMessage(`Reset ${prompt.title} prompt to its shipped default?`, { modal: true, detail: 'Your saved version will be retained in private prompt-recovery files.' }, 'Reset prompt') === 'Reset prompt') {
          await client.request('prompts.reset', { role, expected_hash: prompt.hash });
          this.post({ type: 'notice', message: 'Prompt reset. Previous contents retained in prompt-recovery.' });
        }
        return;
      }
      if (vscode.workspace.textDocuments.some(d => d.uri.fsPath === prompt.path && d.isDirty)) { throw new Error('Save your prompt edits before copying.'); }
      if (!prompt.text.trim()) { throw new Error('This prompt is empty. Edit it or reset it to the shipped default.'); }
      await this.copy(client, this.selected, prompt.text, `${prompt.title} prompt copied. No session mode or permissions changed.`);
    } finally { this.jobs--; void this.background(); }
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

  /** Drain accepted backend work and timers before editor-host shutdown. */
  public async close(): Promise<void> {
    this.switching = true;
    if (this.refreshTimer) { clearInterval(this.refreshTimer); this.refreshTimer = undefined; }
    if (this.initializing) await this.initializing;
    await this.client?.close();
    this.client = undefined;
  }

  /** Release presentation resources and disconnect the backend supervisor pipes. */
  public dispose(): void {
    if (this.refreshTimer) { clearInterval(this.refreshTimer); }
    this.client?.dispose(); this.output.dispose();
  }
}

let activePanel: SessionPanel | undefined;

/** Await backend exit when VS Code deactivates this extension. */
export async function deactivate(): Promise<void> {
  await activePanel?.close();
  activePanel = undefined;
}

/** Register only stable extension APIs available in VS Code 1.85 and later. */
export function activate(context: vscode.ExtensionContext): void {
  const documents = new ReviewDocuments(); const panel = new SessionPanel(context, documents);
  activePanel = panel;
  panel.registerMcp();
  context.subscriptions.push(panel, vscode.workspace.registerTextDocumentContentProvider('repopilotv3-review', documents),
    vscode.window.registerWebviewViewProvider('repopilotv3.sessions', panel),
    vscode.commands.registerCommand('repopilotv3.open', () => vscode.commands.executeCommand('repopilotv3.sessions.focus')),
    vscode.commands.registerCommand('repopilotv3.openHarness', () => panel.openHarness()),
    vscode.commands.registerCommand('repopilotv3.selectWorkspace', () => panel.chooseWorkspace()),
    vscode.commands.registerCommand('repopilotv3.enableMcp', () => panel.integrationAction('mcpEnable')),
    vscode.commands.registerCommand('repopilotv3.mcpStatus', () => panel.integrationAction('mcpStatus')),
    vscode.commands.registerCommand('repopilotv3.generateHarness', () => panel.integrationAction('generateHarness')),
    vscode.commands.registerCommand('repopilotv3.openWorkspaceHarness', () => panel.integrationAction('harnessWorkspace')),
    vscode.commands.registerCommand('repopilotv3.restart', () => panel.restart()),
    vscode.commands.registerCommand('repopilotv3.diagnostics', () => panel.diagnostics()));
}

/** Return dirty file buffers belonging to this workspace, excluding private/escaping paths. */
export function dirtyWorkspaceEditors(root: string): vscode.TextDocument[] {
  return vscode.workspace.textDocuments.filter(document => document.uri.scheme === 'file' && document.isDirty &&
    validSourcePath(path.relative(canonicalPath(root), canonicalPath(document.uri.fsPath)).split(path.sep).join('/')));
}

/** Detect affected unsaved buffers again immediately before patch approval. */
export function dirtyAffectedEditors(root: string, paths: string[]): vscode.TextDocument[] {
  const normalize = pathIdentity;
  const affected = new Set(paths.map(p => normalize(path.join(root, p))));
  return vscode.workspace.textDocuments.filter(d => d.uri.scheme === 'file' && d.isDirty && affected.has(normalize(d.uri.fsPath)));
}
