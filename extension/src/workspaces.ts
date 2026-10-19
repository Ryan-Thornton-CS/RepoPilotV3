/** Explicit project roots independent of the folders listed in a VS Code workspace. */
import * as vscode from 'vscode';
import * as fs from 'fs/promises';
import * as path from 'path';

/** A user-selected filesystem folder, which need not be a top-level VS Code workspace entry. */
export interface ProjectFolder {
  uri: vscode.Uri;
  name: string;
}

/** Validate, restore and browse project roots without editing the editor's workspace file. */
export class ProjectWorkspaces {
  /** Require an existing directory before it becomes the backend's repository boundary. */
  public async resolve(uri: vscode.Uri): Promise<ProjectFolder> {
    if (uri.scheme !== 'file' || uri.query || uri.fragment) {
      throw new Error('Select a filesystem project folder available to this VS Code extension host.');
    }
    try {
      if (!(await fs.stat(uri.fsPath)).isDirectory()) { throw new Error('Not a directory'); }
    } catch {
      throw new Error(`Project folder is unavailable: ${uri.fsPath}. Use Select workspace to choose an existing folder.`);
    }
    return { uri, name: path.basename(uri.fsPath) || uri.fsPath };
  }

  /** Restore an exact saved subfolder; a missing project never falls back to its parent. */
  public async restore(saved: string | undefined, folders: readonly vscode.WorkspaceFolder[] | undefined): Promise<ProjectFolder> {
    if (saved) { return this.resolve(vscode.Uri.parse(saved, true)); }
    if (!folders?.length) { throw new Error('Choose a project through Select workspace, or open a filesystem workspace first.'); }
    return this.resolve(folders[0].uri);
  }

  /** Browse any individual folder, including projects nested inside multi-project workspaces. */
  public async choose(current?: ProjectFolder): Promise<ProjectFolder | undefined> {
    const selected = await vscode.window.showOpenDialog({
      title: 'Select the project folder RepoPilot should use',
      openLabel: 'Use project folder',
      canSelectFiles: false, canSelectFolders: true, canSelectMany: false,
      defaultUri: current?.uri ?? vscode.workspace.workspaceFolders?.[0]?.uri
    });
    return selected?.length ? this.resolve(selected[0]) : undefined;
  }
}
