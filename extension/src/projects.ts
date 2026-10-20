/** Persist explicitly selected folder spaces without moving any repository data. */
import { createHash } from 'crypto';

/** Minimal persistence interface supported by VS Code workspace mementos. */
export interface ProjectStorage {
  get<T>(key: string, fallback: T): T;
  update(key: string, value: unknown): Thenable<void>;
}

/** A stable folder binding and user-editable display name. */
export interface ProjectSpace { id: string; uri: string; name: string; }

/** Maintain one space per canonical folder URI, independent of selected chats. */
export class ProjectRegistry {
  private pending: Promise<unknown> = Promise.resolve();

  /** Serialize registry mutations so concurrent folder additions cannot lose records. */
  private change<T>(operation: () => Promise<T>): Promise<T> {
    const result = this.pending.then(operation);
    this.pending = result.catch(() => undefined);
    return result;
  }
  /** Reuse the editor's host-owned workspace persistence. */
  constructor(private storage: ProjectStorage) {}

  /** Return detached records so UI edits cannot silently mutate persisted state. */
  public list(): ProjectSpace[] {
    return this.storage.get<ProjectSpace[]>('project.spaces', []).map(value => ({ ...value }));
  }

  /** Add a canonical folder, preserving any previously authored project name. */
  public async add(uri: string, name: string): Promise<ProjectSpace> {
    return this.change(async () => {
      await this.storage.update('project.registryInitialized', true);
      const projects = this.list();
      const previous = projects.find(project => project.uri === uri);
      if (previous) { return previous; }
      const project = { id: createHash('sha256').update(uri).digest('hex').slice(0, 20), uri, name };
      await this.storage.update('project.spaces', [...projects, project]);
      return project;
    });
  }

  /** Resolve only a registered identifier supplied by the webview. */
  public get(id: string): ProjectSpace {
    const project = this.list().find(value => value.id === id);
    if (!project) { throw new Error('Project space is unavailable. Add its folder again.'); }
    return project;
  }

  /** Rename display metadata without changing the source root or MCP registration. */
  public async rename(id: string, name: string): Promise<void> {
    return this.change(async () => {
      this.get(id);
      if (!name.trim() || name.trim().length > 160) { throw new Error('Use a project name of 1–160 characters.'); }
      await this.storage.update('project.spaces', this.list().map(p => p.id === id ? { ...p, name: name.trim() } : p));
    });
  }

  /** Remove a listing only; private state and filesystem contents remain untouched. */
  public async remove(id: string): Promise<void> {
    return this.change(async () => {
      this.get(id);
      await this.storage.update('project.spaces', this.list().filter(project => project.id !== id));
    });
  }
}
