/** Feature-detected VS Code MCP registration using only the bundled local backend. */
import * as vscode from 'vscode';
import * as fs from 'fs/promises';
import * as path from 'path';
import * as crypto from 'crypto';
import { canonicalPath, pathIdentity, samePath } from './paths';
import { backendPython } from './runtime';
import { requireTrusted } from './protocol';
import { configuration, changeProperty, readConfiguration, applyConfiguration, ConfigurationEdit } from './configuration';

/** Repository registrations persist across panel disposal without sharing an active root. */
export interface RepositoryRegistration { root: string; state: string; label: string; mode: 'provider' | 'file'; file?: string; signature?: string; portableFiles?: string[]; }
/** Stable stdio definition fields shared across supported editor runtimes. */
interface Definition { label: string; command: string; args: string[]; cwd?: vscode.Uri; version?: string; }
/** Optional stable MCP APIs accessed through a guarded adapter on older editors. */
interface ProviderAPI {
  lm?: { registerMcpServerDefinitionProvider?: (id: string, provider: any) => vscode.Disposable };
  McpStdioServerDefinition?: new (label: string, command: string, args: string[], env: Record<string, string>, version: string) => Definition;
}
/** A discovered RepoPilot definition and its precise JSONC property location. */
interface Existing { file: string; keys: string[]; name: string; definition: any; }

/** Stable per-project server names also serve as generated agent tool-set prefixes. */
export function repositoryServerName(root: string): string {
  const normalized = pathIdentity(root);
  return 'repopilotv3-' + crypto.createHash('sha256').update(normalized).digest('hex').slice(0, 10);
}

/** Recognize RepoPilot configurations without treating unrelated integrations as managed. */
export function isRepoPilot(name: string, definition: any): boolean {
  return /repopilot/i.test(name) || Array.isArray(definition?.args) && definition.args.some((arg: unknown) => typeof arg === 'string' && /^repopilot(?:v[23])?$/.test(arg));
}

/** Own provider definitions and explicit, previewed file-based fallbacks. */
export class McpIntegration implements vscode.Disposable {
  private changed = new vscode.EventEmitter<void>();
  private provider?: vscode.Disposable;
  private watcher?: vscode.FileSystemWatcher;
  private api = vscode as unknown as ProviderAPI;
  /** Register capabilities without executing Python or enabling any repository automatically. */
  constructor(private context: vscode.ExtensionContext, private preview: (edits: ConfigurationEdit[]) => Promise<boolean>) {
    if (this.supported) {
      this.provider = this.api.lm!.registerMcpServerDefinitionProvider!('repopilotv3.repositories', {
        onDidChangeMcpServerDefinitions: this.changed.event,
        provideMcpServerDefinitions: () => this.definitions(),
        resolveMcpServerDefinition: async (definition: Definition) => {
          requireTrusted(vscode.workspace.isTrusted);
          const current = (await this.definitions()).find(item => item.label === definition.label);
          if (!current) { throw new Error('This repository registration is disabled or conflicts with an existing MCP entry.'); }
          return current;
        }
      });
      this.watcher = vscode.workspace.createFileSystemWatcher('**/{.mcp.json,.vscode/mcp.json,*.code-workspace}');
      this.watcher.onDidChange(() => this.changed.fire()); this.watcher.onDidCreate(() => this.changed.fire()); this.watcher.onDidDelete(() => this.changed.fire());
    }
    context.subscriptions.push(vscode.workspace.onDidGrantWorkspaceTrust(() => this.changed.fire()));
  }
  /** Detect the stable MCP API independently of the panel's older minimum editor version. */
  public get supported(): boolean { return typeof this.api.lm?.registerMcpServerDefinitionProvider === 'function' && typeof this.api.McpStdioServerDefinition === 'function'; }
  /** Return persisted explicit registrations; opening the panel does not enable servers. */
  public registrations(): RepositoryRegistration[] { return this.context.workspaceState.get<RepositoryRegistration[]>('mcp.repositories', []); }
  private async save(rows: RepositoryRegistration[]): Promise<void> { await this.context.workspaceState.update('mcp.repositories', rows); this.changed.fire(); }

  private candidates(root: string): string[] {
    root = canonicalPath(root);
    const enclosing = canonicalPath(vscode.workspace.getWorkspaceFolder(vscode.Uri.file(root))?.uri.fsPath ?? root);
    const result: string[] = [];
    let current = root;
    while (true) {
      result.push(path.join(current, '.mcp.json'), path.join(current, '.vscode/mcp.json'));
      if (current === enclosing || path.dirname(current) === current || !path.relative(enclosing, current) || path.relative(enclosing, current).startsWith('..')) { break; }
      current = path.dirname(current);
    }
    if (vscode.workspace.workspaceFile?.scheme === 'file') { result.push(vscode.workspace.workspaceFile.fsPath); }
    return [...new Set(result)];
  }

  /** Inspect selected/enclosing workspace definitions, including comments and trailing commas. */
  public async existing(root: string): Promise<Existing[]> {
    root = canonicalPath(root);
    const result: Existing[] = [];
    for (const file of this.candidates(root)) {
      const text = await readConfiguration(file); if (text === null) { continue; }
      const data = configuration(text);
      const keys = file.endsWith('.code-workspace') ? ['mcp', 'servers'] : path.basename(file) === '.mcp.json' ? ['mcpServers'] : ['servers'];
      const entries = keys.reduce((value: any, key) => value?.[key], data);
      if (entries !== undefined && (!entries || typeof entries !== 'object' || Array.isArray(entries))) { throw new Error(`Invalid MCP server object in ${file}`); }
      for (const [name, definition] of Object.entries(entries ?? {})) {
        if (!isRepoPilot(name, definition)) { continue; }
        const item: any = definition;
        const index = Array.isArray(item?.args) ? item.args.indexOf('--repo') : -1;
        const repo = index >= 0 ? item.args[index + 1] : undefined;
        const base = path.basename(path.dirname(file)) === '.vscode' ? path.dirname(path.dirname(file)) : path.dirname(file);
        const resolved = typeof repo === 'string' && !repo.includes('${') ? path.resolve(base, repo) : repo === '${workspaceFolder}' ? base : undefined;
        const same = resolved === undefined || samePath(resolved, root);
        if (same) { result.push({ file, keys, name, definition }); }
      }
    }
    return result;
  }

  /** Rebuild launch paths from the currently installed VSIX without creating repository state. */
  public async definitions(): Promise<Definition[]> {
    if (!this.supported || !vscode.workspace.isTrusted) { return []; }
    const result: Definition[] = [];
    for (const row of this.registrations().filter(item => item.mode === 'provider')) {
      try {
        if ((await this.existing(row.root)).some(entry => !row.portableFiles?.some(file => samePath(file, entry.file))) || !(await fs.stat(row.root)).isDirectory()) { continue; }
        const python = await backendPython('bundled', this.context.extensionUri.fsPath);
        const definition = new this.api.McpStdioServerDefinition!(row.label, python, this.arguments(row.root, row.state),
          { PYTHONUTF8: '1', PYTHONDONTWRITEBYTECODE: '1' }, this.context.extension.packageJSON.version);
        definition.cwd = vscode.Uri.file(row.root); result.push(definition);
      } catch { /* A missing/conflicting project is reported through status rather than executing it. */ }
    }
    return result;
  }

  private arguments(root: string, state: string): string[] {
    return ['-I', '-B', '-u', '-m', 'repopilotv3', '--repo', root, ...(state ? ['--state', state] : []), 'mcp', 'serve'];
  }

  /** Enable one selected project only after duplicate handling and any concrete file preview. */
  public async enable(root: string, state = ''): Promise<void> {
    requireTrusted(vscode.workspace.isTrusted); root = canonicalPath(root);
    const found = await this.existing(root);
    let portableFiles: string[] | undefined;
    if (found.length) {
      const options = ['Keep existing configuration', 'Switch to extension-managed setup'];
      if (found.every(entry => entry.keys[0] === 'mcpServers')) { options.splice(1, 0, 'Keep portable config and enable VS Code MCP'); }
      const choice = await vscode.window.showQuickPick(options, { title: `RepoPilot MCP already configured for ${root}` });
      if (choice === 'Keep portable config and enable VS Code MCP') { portableFiles = [...new Set(found.map(entry => entry.file))]; }
      else if (choice !== 'Switch to extension-managed setup') { return; }
    }
    let mode: 'provider' | 'file' = this.supported ? 'provider' : 'file';
    if (mode === 'file') {
      const commands = await vscode.commands.getCommands(true);
      if (!commands.some(name => /^workbench\.mcp\./.test(name))) { throw new Error('This VS Code version does not expose MCP support. The RepoPilot panel still works; upgrade the editor to use MCP.'); }
      if (await vscode.window.showInformationMessage('The MCP provider API is unavailable. Preview a workspace MCP configuration instead?', 'Preview configuration') !== 'Preview configuration') { return; }
    }
    const edits = new Map<string, ConfigurationEdit>();
    for (const entry of found.filter(item => !portableFiles?.includes(item.file))) {
      let edit = edits.get(entry.file);
      if (!edit) { const before = await readConfiguration(entry.file); edit = { file: entry.file, before, after: before ?? '{}' }; edits.set(entry.file, edit); }
      edit.after = changeProperty(edit.after, [...entry.keys, entry.name], undefined);
    }
    const label = repositoryServerName(root);
    let file: string | undefined;
    let signature: string | undefined;
    if (mode === 'file') {
      file = path.join(root, '.vscode/mcp.json');
      const before = await readConfiguration(file);
      const edit = edits.get(file) ?? { file, before, after: before ?? '{\n  "servers": {}\n}\n' };
      const definition = { type: 'stdio', command: await backendPython('bundled', this.context.extensionUri.fsPath), args: this.arguments(root, state) };
      signature = JSON.stringify(definition);
      edit.after = changeProperty(edit.after, ['servers', label], definition);
      edits.set(file, edit);
    }
    if (edits.size) {
      if (!await this.preview([...edits.values()])) { return; }
      requireTrusted(vscode.workspace.isTrusted);
      await applyConfiguration([...edits.values()], this.context.globalStorageUri.fsPath);
    }
    requireTrusted(vscode.workspace.isTrusted);
    await this.save([...this.registrations().filter(row => !samePath(row.root, root)), { root, state, label, mode, ...(file ? { file, signature } : {}), ...(portableFiles ? { portableFiles } : {}) }]);
    void vscode.window.showInformationMessage(`RepoPilot MCP enabled for ${root}. Enable its tools in your chat tool picker; VS Code retains its normal trust/start controls.`);
  }

  /** Disable one registration without changing other repositories or unrelated MCP entries. */
  public async disable(): Promise<void> {
    requireTrusted(vscode.workspace.isTrusted);
    const rows = this.registrations();
    const chosen = await vscode.window.showQuickPick(rows.map(row => ({ label: row.label, description: row.root, row })), { title: 'Disable RepoPilot MCP for a project' });
    if (!chosen) { return; }
    const row = chosen.row;
    if (row.mode === 'file' && row.file) {
      const before = await readConfiguration(row.file);
      if (before !== null) {
        const definition = configuration(before).servers?.[row.label];
        if (definition && (!row.signature || JSON.stringify(definition) !== row.signature)) { throw new Error('The MCP entry changed; inspect it before disabling.'); }
        const edit = { file: row.file, before, after: changeProperty(before, ['servers', row.label], undefined) };
        if (!await this.preview([edit])) { return; }
        requireTrusted(vscode.workspace.isTrusted);
        await applyConfiguration([edit], this.context.globalStorageUri.fsPath);
      }
    }
    requireTrusted(vscode.workspace.isTrusted);
    await this.save(rows.filter(item => item.root !== row.root));
  }

  /** Show registration facts without claiming access to private Copilot connection state. */
  public async status(root: string): Promise<void> {
    const runtime = await backendPython('bundled', this.context.extensionUri.fsPath);
    const registrations = await Promise.all(this.registrations().map(async row => {
      const text = row.file ? await readConfiguration(row.file) : null;
      const entry = text ? configuration(text).servers?.[row.label] : undefined;
      return { ...row, stale_runtime: row.mode === 'file' && entry?.command !== runtime };
    }));
    const data = { selected_project: root, provider_api_available: this.supported, registrations,
      selected_existing_entries: await this.existing(root),
      available_definitions: (await this.definitions()).map(item => ({ label: item.label, command: item.command, args: item.args })),
      connection: 'Registration is observable here. Inspect MCP: List Servers in VS Code for actual start/connection state.' };
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({ content: JSON.stringify(data, null, 2), language: 'json' }));
  }

  /** Inspect existing configurations, or a copyable configuration for the selected bundled server. */
  public async configuration(root: string, state = ''): Promise<void> {
    const found = await this.existing(root);
    if (found.length) {
      const selected = await vscode.window.showQuickPick([...new Set(found.map(item => item.file))], { title: 'Open MCP configuration' });
      if (selected) { await vscode.window.showTextDocument(vscode.Uri.file(selected)); } return;
    }
    const data = { servers: { [repositoryServerName(root)]: { type: 'stdio', command: await backendPython('bundled', this.context.extensionUri.fsPath), args: this.arguments(root, state) } } };
    await vscode.window.showTextDocument(await vscode.workspace.openTextDocument({ content: JSON.stringify(data, null, 2), language: 'json' }));
  }

  /** Release provider registrations and event listeners; server lifecycle remains VS Code-owned. */
  public dispose(): void { this.provider?.dispose(); this.watcher?.dispose(); this.changed.dispose(); }
}
