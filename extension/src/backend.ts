/** Local stdio transport. Connection loss rejects work without replaying any request. */
import { ChildProcessWithoutNullStreams, spawn } from 'child_process';
import { EventEmitter } from 'events';
import * as fs from 'fs';
import * as path from 'path';

/** Backend compatibility information returned before the panel performs management work. */
export interface BackendHello {
  bridge_version: string;
  backend_version: string;
  workspace: string;
  initialized: boolean;
}

/** One Python process bound to one explicit workspace and host-owned state directory. */
export class BackendClient extends EventEmitter {
  private process?: ChildProcessWithoutNullStreams;
  private sequence = 0;
  private closing = false;
  private buffer = '';
  private pending = new Map<number, { resolve: (v: any) => void; reject: (e: Error) => void; method: string; started: number }>();
  private connection?: Promise<BackendHello>;
  private canonicalRoot?: string;
  public hello?: BackendHello;

  /** Capture an executable and argument-array configuration; never invoke a shell. */
  constructor(public readonly root: string, private python: string, private state: string,
              private log: (text: string) => void) { super(); }

  /** Launch once and verify protocol/backend compatibility before any session request. */
  public connect(): Promise<BackendHello> {
    if (this.connection) { return this.connection; }
    this.connection = this.launch();
    return this.connection;
  }

  private async launch(): Promise<BackendHello> {
    const started = performance.now();
    this.closing = false;
    const args = ['-I', '-B', '-u', '-m', 'repopilotv3', '--repo', this.root];
    if (this.state) { args.push('--state', this.state); }
    args.push('bridge', 'serve');
    this.process = spawn(this.python, args, { cwd: this.root, shell: false, windowsHide: true,
      env: { ...process.env, PYTHONUTF8: '1', PYTHONIOENCODING: 'utf-8', PYTHONDONTWRITEBYTECODE: '1' } });
    this.process.stdout.setEncoding('utf8');
    this.process.stderr.setEncoding('utf8');
    this.process.stdout.on('data', (text: string) => this.consume(text));
    this.process.stderr.on('data', (text: string) => this.log(text.slice(0, 16000)));
    this.process.on('error', (error: Error) => this.fail(error));
    this.process.on('exit', (code, signal) => {
      if (!this.closing) { this.fail(new Error(
        `Backend disconnected (${code ?? signal}). Inspect history and interrupted operations before retrying. Nothing was replayed.`)); }
    });
    const timeout = setTimeout(() => this.fail(new Error('Backend handshake timed out; check Output → RepoPilotV3 and the bundled runtime.')), 20000);
    try {
      const hello = await this.request('hello');
      const normalize = (value: string): string => process.platform === 'win32' ? path.resolve(value).toLowerCase() : path.resolve(value);
      if (hello.bridge_version !== '1.0' || !/^0\.2\./.test(hello.backend_version) || typeof hello.workspace !== 'string' || normalize(hello.workspace) !== normalize(fs.realpathSync(this.root))) {
        throw new Error('Incompatible backend: reinstall the matching VSIX or check your custom Python override.');
      }
      this.hello = hello;
      this.canonicalRoot = hello.workspace;
      this.log(`Backend ready in ${Math.round(performance.now() - started)} ms.`);
      return hello;
    } catch (error) {
      this.dispose();
      throw error;
    } finally { clearTimeout(timeout); }
  }

  /** Send one management call. No implicit retry, including read calls, occurs on disconnect. */
  public request(method: string, parameters: Record<string, unknown> = {}): Promise<any> {
    if (!this.process || this.process.killed || !this.process.stdin.writable) {
      return Promise.reject(new Error('Backend is disconnected. Use Reconnect Backend; no work is replayed.'));
    }
    const id = ++this.sequence;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject, method, started: performance.now() });
      const message = { jsonrpc: '2.0', id, method,
        params: { ...parameters, workspace: this.canonicalRoot ?? this.root, bridge_version: '1.0' } };
      this.process!.stdin.write(JSON.stringify(message) + '\n', error => {
        if (error) { this.pending.delete(id); reject(error); }
      });
    });
  }

  private consume(text: string): void {
    this.buffer += text;
    if (Buffer.byteLength(this.buffer) > 32 * 1024 * 1024) {
      this.fail(new Error('Backend frame exceeded 32 MiB. Inspect saved results.')); this.dispose(); return;
    }
    let index: number;
    while ((index = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, index); this.buffer = this.buffer.slice(index + 1);
      try {
        const message = JSON.parse(line);
        if (message.jsonrpc !== '2.0') { throw new Error('Invalid backend frame'); }
        if (message.method === 'progress') { this.emit('progress', message.params); continue; }
        const pending = this.pending.get(message.id);
        if (!pending) { continue; }
        this.pending.delete(message.id);
        const elapsed = Math.round(performance.now() - pending.started);
        if (elapsed >= 500) { this.log(`Slow local request: ${pending.method} took ${elapsed} ms (includes queue/transport time).`); }
        if (message.error) { pending.reject(new Error(`${message.error.data?.code ?? 'backend_error'}: ${message.error.message}`)); }
        else { pending.resolve(message.result); }
      } catch (error) { this.fail(new Error('Invalid backend protocol output: ' + String(error))); }
    }
  }

  private fail(error: Error): void {
    for (const p of this.pending.values()) { p.reject(error); }
    this.pending.clear();
    this.log(error.message);
    this.emit('disconnected', error.message);
  }

  /** Wait for the old writer to exit before an explicit reconnect starts its replacement. */
  public async close(): Promise<void> {
    const child = this.process;
    if (!child || child.exitCode !== null || child.signalCode !== null) { this.dispose(); return; }
    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        child.removeListener('close', finished);
        reject(new Error('Previous backend is still finishing accepted work. Wait before reconnecting; nothing was replayed.'));
      }, 10000);
      const finished = (): void => { clearTimeout(timer); resolve(); };
      child.once('close', finished);
      this.dispose();
    });
  }

  /** Close stdin gracefully so accepted work drains; never replay it on a later connection. */
  public dispose(): void {
    this.closing = true;
    this.process?.stdin.end();
    this.connection = undefined;
    this.hello = undefined;
    this.canonicalRoot = undefined;
  }
}
