# Setup and troubleshooting

## Install the single VSIX

Choose **Extensions: Install from VSIX**, select `repopilotv3-0.2.14.vsix`, and reload VS Code if prompted. Open a trusted local folder and click RepoPilot's icon. The backend initializes private state automatically. No external Python, pip, uv, installer, internet connection or account is needed.

The same VSIX contains CPython 3.12.14 and fully installed dependencies for **Windows x64** and **Linux/WSL x64**. Linux needs a glibc-based x64 distribution compatible with the bundled manylinux2014 wheels; Alpine/musl is unsupported. macOS and ARM are not included. Install in the **WSL/remote extension host** when working in a Remote WSL window. The extension selects that host's runtime, not the UI computer's runtime.

The panel uses stable VS Code **1.85+** APIs. Copilot agent/handoff, MCP and hook support depend separately on the installed editor/Copilot versions. Native Windows is recommended for actual Inventor add-ins. SDKs, Framework reference assemblies and Inventor DLLs remain host tools; Python.NET bindings are already bundled. See [C#/Inventor](csharp-inventor.md).

First use **Prompts → Copy project prompt** and paste it into the WebUI project's reusable instructions. This works before creating a session. The standalone guidance defines behavior and tools with an enforced 8,000-character ceiling (measured on generation). The same prompt is exported as 00_PROJECT_INSTRUCTIONS.md.

Then click **New session**, paste its short starter into a new chat in that project, and **Paste + Run** the model’s task/TODO reply. The starter contains session identity and the task introduction or continuation; it does not repeat the project prompt, tool catalog or repository map. Copy the reusable prompt again when its version changes; local updates never change external project settings automatically.

Chat, Prompts, Execution, TODO, the context ring and **Context files** sit below the composer; project/MCP/maintenance settings stay in the cogwheel. See the [walkthrough](walkthrough.md). **Execution → Validation profile → python** explicitly approves pytest execution; pytest is already bundled. Initialization never runs tests or modifies `.github`/MCP files.

## Select a project inside a larger workspace

Use **Projects → Add project folder**, browse to the specific project directory, and choose **Use project folder**. You can select a subfolder or another accessible folder; it does not need to be a top-level entry in your VS Code workspace. RepoPilot leaves your workspace file and other open projects unchanged. The options menu shows the exact selected path, and RepoPilot remembers it across reloads. A missing saved folder produces an actionable error rather than silently switching back to a parent directory.

Each selected folder uses its own private state and sessions; existing chats stay with their original folder. A custom `statePath` must be unique to the project. Create a new chat after switching projects and use that chat’s starter so the external model receives the correct source context. Only the selected folder is indexed; sibling projects are excluded. Hover menu items, checkboxes and chat/context actions for concise descriptions.

## Copilot and CLI

**Settings → Copilot & MCP → Enable MCP for selected project** registers the selected folder using the bundled interpreter. **Generate / update workspace harness** previews agents, instructions and hooks before explicit application. **Open bundled harness** keeps manual copying available; **Open workspace .github** opens the generated/current project files. Plain CLI/MCP setup never writes these files. See [harness setup](../harness/README.md).

To locate the runtime, find RepoPilotV3's installed extension folder using VS Code's **Developer: Open Extensions Folder**. Under it use `backend/win32-x64/python/python.exe` on Windows or `backend/linux-x64/python/bin/python3.12` on Linux. This executable can also run `-I -m repopilotv3 --help`. Absolute harness paths must be adjusted after changing the extension version/location. A source checkout can instead use `uv sync --frozen --group dev --extra csharp` and its `.venv`.

## Settings and workspaces

| Setting | Default | Meaning |
|---|---|---|
| `repopilotv3.pythonPath` | `bundled` | Optional absolute executable override containing backend 0.2.x |
| `repopilotv3.statePath` | empty | Host-owned `.repopilotv3/` under the selected root |
| `repopilotv3.autoCopyResponses` | `true` | Copy completed user-triggered responses only |
| `repopilotv3.refreshSeconds` | `5` | Active-package polling interval, 2–60 seconds |

Use **+ Project** for multi-root projects. Separate roots/hosts need separate SQLite state directories; never share a live database between Windows and WSL. V3 does not migrate V1/V2 state. Move the view beside Chat using VS Code's standard view-container actions.

## If something fails

- **Backend launch:** check **Output → RepoPilotV3**, Workspace Trust and the extension-host architecture. Leave `pythonPath` at `bundled`; clear an old explicit path if it points to a removed environment. Reinstall the VSIX if bundled files are missing. Corporate policies may block bundled executables and require administrator approval.
- **Disconnect:** use **Reconnect Backend**. It reads persisted history and never replays a request, patch or validation. Inspect uncertain operations before continuing.
- **Ownership:** choose a different absolute `statePath` for another host/root. Do not edit database ownership fields.
- **`todo_required`:** this chat's model must call `todo`, even when continuing an existing task.
- **`request_conflict`:** an ID was reused with different JSON. Use a new ID. An identical request returns its recorded result.
- **`request_interrupted`:** inspect source, evidence and patch journal before a new request; never blindly replay a mutation.
- **Patch stale/dirty editor:** save explicitly and refresh context; ask for a new proposal with current hashes. Native diffs are read-only and approval covers the whole proposal. Recovery only repairs interrupted application and refuses to overwrite later edits.
- **Validation:** no approved profile means no tests executed. A successful check differs from test evidence. Missing JUnit/TRX is inconclusive; source/config changes make previous evidence historical.
- **Clipboard:** expand/select JSON or open the saved outbox file if the native clipboard fails. Background refresh never copies. Confirm only actual external transfers, not local copies.
- **Harness discovery:** manually choose one MCP format and verify native tool/agent/hook support in Copilot's logs. Partial hook coverage is reported; stop hooks never force continuation.

RepoPilot makes no model calls or telemetry requests, and never installs its own runtime dependencies during operation. A session-authorized command may restore project dependencies or use the network. Approved validation commands and project build targets can perform their own network activity. Dependencies/runtimes are provisioned only when a developer explicitly builds the VSIX.

## Commands and chat controls

New chats introduce their identity and ask for the objective. The model names the chat and maintains task/TODO through tools. Send executes detected protocol requests; ordinary text is prepared for the WebUI. Malformed request-shaped text returns an error. Paste + Run reads clipboard replies. Run as model reply explicitly imports textbox content; Copy message for WebUI forwards examples without execution.

The first run_command request presents a local grant for this session. Approved commands execute with your account permissions, can modify files/use the network, and remain authorized until revoked through Execution. Stop cancels the command tree. Future sessions, including continuations, need a new grant. Source changes proposed through patches always await whole-proposal approval.

The context meter shows observed estimates against a per-chat workflow threshold; clicking it changes the default 40,000 budget. Context files opens the automatically maintained upload checklist; it does not regenerate files just to display it. Threshold advisories prepare handoffs without stopping work. **Continue in new chat** appears at the threshold: open the external chat, confirm, and the local continuation opens with its handoff ready to copy. It preserves the task but gets a new chat identifier and requires initial TODO readiness and command permission. Existing V3 sessions preserve saved limits and permissions, defaulting to 40k and no command grant when absent; their history/tasks/drafts remain intact.

## Repository busy during startup

Session replies display before routine package refresh and clipboard/diff work. Active-session packages refresh in the background, including repository orientation before a task exists. Foreground changes schedule a refresh, and polling detects external edits. Overlapping requests coalesce and busy writers defer refreshes. **Context files** only opens the checklist; failures expose **Retry refresh** without blocking chat navigation. If opening a session is slow, inspect **Output → RepoPilotV3**: backend startup and session restoration have timings; requests over 500 ms show their method and elapsed time. Client timings include queue/transport delays; handler timings exclude the queue. Slow clipboard copies are reported separately. No message bodies are logged. First source indexing, Roslyn/MSBuild loading, remote filesystems and editor/OS startup can still take time; timings identify which part is waiting.

Version 0.2.14 shares backend initialization across overlapping panel events and waits for normal writer contention. Background context refresh quietly defers when another writer is active. Session deletion uses a separate management queue and does not wait for context exports. Status reads only the snapshot identity rather than parsing historical analysis. Reconnect waits for its previous backend to exit; it never replays an edit or command. Open the actual source folder rather than your entire home directory. Editor extensions, caches, credentials and user dependency stores are excluded from scanning; parent-folder scans also honor nested repositories' Git ignore files.

After upgrading, reload **all VS Code windows using RepoPilot** so an old backend cannot keep indexing the same workspace. If a long operation still holds the writer for over 30 seconds, the foreground message explains how to finish or close that older window and retry. Do not delete `writer.lock`: operating-system ownership releases it when its process exits. Status and history stay available while work is queued.

Archive, restore or delete chats from **Sessions**. Deletion is a local action with confirmation; it is unavailable to model tools. A running command or interrupted patch requiring recovery blocks deletion. Deleting a chat retains shared task state and provenance/recovery material; it is not a secure erase of immutable packages or logs.

After confirmation the deleted row disappears immediately, independently of the next history refresh. A durable private marker prevents it from reappearing even if an index transaction delays cleanup; pending database cleanup resumes on backend restart. A session actively executing a tool is kept with an immediate actionable message, rather than queuing deletion behind that operation. Other sessions can still be deleted.

For invalid hunk counts, ask the model to use `propose_patch` with `files` instead of `diff`. Send complete contents for each changed file, with current snapshot/base hashes. RepoPilot generates the diff locally; normal native review and approval follow. After updating the extension, use **Prompts → Copy project prompt** to replace your WebUI project instructions and open **Context files** to check the automatically refreshed protocol/schema attachments. Local regeneration does not confirm those external transfers.

Automatic export retention checks run during package preparation/reuse, at most once a minute by default. When generated export folders plus their artifact cache exceed **512 MiB**, cleanup removes oldest superseded generations and unreachable cache entries toward **384 MiB**. Every existing chat's current package (including archived chats) and two newest generations remain protected. Deleted chats no longer pin generated source exports. Current manifests and upload receipts remain valid; task/history databases, validation logs, source snapshots, patch proposals and recovery copies are outside this cleanup scope. Protected packages can exceed the soft limit; diagnostics report this instead of deleting them. Previously opened old export folders may disappear after retention; use the current package folder.

Configure the private `.repopilotv3/config.json` (or your configured external state location):

```json
"export_cleanup": {
  "enabled": true,
  "max_bytes": 536870912,
  "target_bytes": 402653184,
  "keep_generations": 2,
  "interval_seconds": 60
}
```

`target_bytes` must not exceed `max_bytes`. Set `enabled` to false to disable retention. Existing configurations receive these defaults in memory without rewriting your file. **Diagnostics** reports the policy and last cleanup's freed/remaining bytes and any warning; the same local report lives in `exports/cleanup.json`. Cleanup runs under the existing repository writer lock, refuses symlinked/corrupt storage, and reports errors without making a completed export fail. It never removes external WebUI attachments or confirms transfers.

The cogwheel contains Projects, Copilot & MCP and Maintenance. Chat, Prompts, Execution, TODO and Context files are beside the composer. Each bottom menu opens upward and closes with Escape. Integration setup works before any manual chat exists. Project selection is shared; enabling MCP remains explicit per project. Switching the panel preserves old project registrations/process boundaries. Disable individual roots through **Disable a project MCP**. MCP status reports registration/configuration facts, not private Copilot connection state; use VS Code's built-in MCP server controls to start/approve servers and select tools.

After an upgrade, native-provider definitions resolve the current bundled executable automatically. Generated hooks and file-based launch paths require explicit previewed updates when stale. A conflicting or malformed existing configuration blocks setup with an actionable error. MCP configuration changes have recovery journals under the extension's private global storage `mcp-recovery/`; failed writes roll back when originals are still safe to restore. Never edit an old recovery journal and replay it blindly. Existing custom Python backends need the matching release for the new management methods; choose bundled Python otherwise.

If a portable `.mcp.json` belongs to a separate local-model client, choose **Keep portable config and enable VS Code MCP**. That explicit choice leaves the portable file unchanged and registers the selected project separately with VS Code. Native `.vscode/mcp.json` duplicates still require migration or keeping the existing server.

## Project spaces and editable prompts

Use **Projects → + Project** to add any existing folder, including a nested project. Each space retains its own sessions, selected chat, drafts, packages and configuration. Project names are local display metadata. A missing folder remains listed with an actionable error; it never falls back to its parent. **Remove project space** removes only the listing, preserving private state and independently registered MCP servers. Finish or explicitly stop current work before switching.

**Sessions** opens the chat interface; **Context files** lists the current chat’s immutable upload artifacts and their upload/replace/keep status. Open files or folders directly and confirm actual transfers. Local refresh is automatic; Retry refresh appears on failure. **Documentation** has searchable offline help with annotated screenshots.

Use **Prompts** beside the composer for independent role guidance. Copy an independent role prompt, or edit `.repopilotv3/prompts/{planning,asking,implementation,review}.md` (under custom private state when configured). Save before copying. These files are authoritative and upgrades do not overwrite them. **Reset prompt to default** confirms the change and saves previous bytes under `prompt-recovery/`. Back up both directories with the project’s private state; source retrieval excludes them. The static Copilot agent files remain independent.

**TODO → Reset checklist** archives task items in revision history and preserves objective, requirements and validation. It affects continuation chats sharing that task, invalidates their TODO readiness, and prepares a message asking the model for a replacement checklist. Use new request IDs when rebuilding it. This is a local action, not a model tool.


The **Settings** menu is anchored below its cogwheel. Press **Escape** to close any custom menu (including the session drawer, composer actions, TODO and upload checklist); focus returns to its opening control. Clicking outside closes menus too. Escape does not stop commands or send messages.

The circular **context indicator** fills against the selected chat’s configured advisory limit. Hover or focus it to see **Context window: % full** and **used/limit tokens used**. These are locally recorded estimates, not measured WebUI usage. Clicking the circle changes the limit. At/above the limit the ring stays full while hover text shows the actual percentage, including values over 100%.
