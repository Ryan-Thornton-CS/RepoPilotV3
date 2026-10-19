/** Read-only virtual documents for reviewing a complete immutable patch proposal. */
import * as vscode from 'vscode';

/** Native diff input for one proposal file. */
export interface ProposalFile {
  path: string;
  original: string;
  proposed: string;
  status: string;
}

/** Supply text through a non-file scheme; VS Code cannot save these documents to source. */
export class ReviewDocuments implements vscode.TextDocumentContentProvider {
  private documents = new Map<string, string>();

  /** Store a particular proposal side and return its read-only URI. */
  public uri(session: string, patch: string, file: ProposalFile, side: 'original' | 'proposed'): vscode.Uri {
    const uri = vscode.Uri.from({ scheme: 'repopilotv3-review', authority: session,
      path: `/${patch}/${side}/${file.path}` });
    this.documents.set(uri.toString(), file[side]);
    return uri;
  }

  /** Return only previously registered proposal data; no arbitrary filesystem lookup occurs. */
  public provideTextDocumentContent(uri: vscode.Uri): string {
    return this.documents.get(uri.toString()) ?? 'Review expired. Reopen the proposal from its session.';
  }
}
