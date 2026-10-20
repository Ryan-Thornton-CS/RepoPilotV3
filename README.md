# RepoPilotV3

A local VS Code panel for using a web-only model as a development assistant. Paste its complete reply, review requested actions, and copy the tool response back. RepoPilot tracks sessions and TODOs, prepares source context, and shows native diffs before applying a proposal. It makes **no model API calls**.

Copilot uses four structured agents with native development tools plus focused RepoPilot MCP retrieval/checkpoints and repository setup/status. Use **Settings → Copilot & MCP** to explicitly register the bundled server and preview/generate the workspace harness. The authoritative static bundle remains available for manual copying.

V3 is independent of RepoPilot and RepoPilotV2. There is no state migration or Textual TUI.

## Install

In VS Code, choose **Extensions: Install from VSIX** and select **[repopilotv3-0.2.14.vsix](dist/repopilotv3-0.2.14.vsix)**. Open a trusted source folder and click the RepoPilot icon. That's the entire backend setup.

The VSIX includes **Python 3.12, the backend, pinned dependencies, pytest, C# analysis bindings, documentation, and the static Copilot harness** for **Windows x64 and Linux/WSL x64**. It runs offline without installing Python or downloading dependencies. VS Code **1.85+** is required; Copilot agents/MCP/hooks have separate host requirements. macOS and ARM hosts are not bundled.

SDKs, Framework reference assemblies and Autodesk interop DLLs remain optional host tools for C#/Inventor projects. Copilot harness files can be generated after a reviewed preview or copied manually through **Open Harness Folder**; the manual panel works immediately without them. See [setup and troubleshooting](docs/setup.md).

The standalone [WebUI project prompt](src/repopilotv3/resources/webui.md) defines behavior and manual tools. Use **Prompts → Copy project prompt** and paste it once into your WebUI project's instructions. It has an enforced 8,000-character ceiling, and contains no session identity or repository source.

The top-level **Projects / Documentation** tabs provide folder spaces and searchable illustrated help. Each project contains **Sessions / Context files**. Existing sessions stay in their original folder; removing a project listing never deletes its source or state.

The normal loop is **New session → copy starter → paste model reply → review changes → copy tool results**.

1. **New session** opens a chat immediately. Paste its short starter into a new chat within your configured WebUI project. It supplies this session's identity and asks for the objective; the model names the chat and creates its task/TODO using the separate project instructions.
2. **Send** detects and executes pasted tool requests; ordinary prose becomes a message for the WebUI. **Paste + Run** imports from the clipboard. The composer menu offers **Copy message for WebUI** when you want to share JSON examples without execution.
3. Use **Prompts → Copy Planning/Asking/Implementation/Review prompt** for role guidance. **Edit prompts** opens a project-owned Markdown file; saved edits are authoritative. Resetting a prompt keeps a recovery copy. These are prompts, not session modes.
4. Source proposals automatically open native diff tabs. **Approve whole proposal** applies the change and runs configured validation. Commands request local permission once per chat and offer **Stop** while running.
5. **Context files**, beside the context meter, opens the automatically maintained upload/replace/keep checklist. At the threshold, **Continue in new chat** opens a continuation after confirmation, with the saved handoff ready to copy.

The bottom bar groups Chat, Prompts, Execution and TODO controls. The cogwheel holds project, Copilot/MCP and maintenance settings. Use the project selector’s adjacent menu to rename or remove a project listing. Hover the circular context indicator for percentage and token counts; click it to adjust the limit. Escape closes open menus and restores focus. Context tracking is an observed estimate with a configurable 40k advisory threshold, not a measurement of remote model capacity.

Drag the RepoPilot view container or use VS Code's **Move View / Move View Container** actions to put it in the Secondary Side Bar or Panel beside Chat/Codex. It is an independent view, not embedded in another extension.

## What's included

- SQLite/FTS5 incremental search, cited source reads, focused context, cached analysis, and optional Python-hosted Roslyn with labeled text fallback.
- Static Plan, Code, Ask, and Review agents (~4.3–4.7k estimated tokens each), optional handoffs, compact shared instructions, warning-only hooks.
- Persistent searchable/renamable/archiveable sessions, task-specific TODOs, drafts, private transcripts, local notes, rough observed-context estimates.
- Protocol 1.0, complete prose/JSON replies, model task/chat naming, sequential request queues, local approvals and deduplication.
- Permission-bound commands with streaming output, cancellation, durable logs and observed source diffs; editable role prompts, standalone project instructions and separate session introductions.
- Automatic size-based export/cache retention, preserving current packages and recovery evidence.
- Complete-file proposals with locally generated diffs, plus legacy exact UTF-8 patches, whole-proposal native diffs, dirty-editor guards, freshness checks, recovery copies and rollback.
- Named local validation profiles, actual JUnit/TRX test evidence, snapshot/configuration provenance, historical-result detection.
- Immutable context generations, unchanged-artifact reuse, focused and optional full-source exports, paste/upload/keep/replace checklist and scoped confirmations.
- A versioned local stdio management API, separate from the six-tool setup/status/retrieval/checkpoint MCP surface.

## Documentation

| Guide | Purpose |
|---|---|
| [Setup and troubleshooting](docs/setup.md) | VSIX, Windows/WSL, settings and recovery |
| [Static harness](harness/README.md) | Previewed generation, manual placement and MCP executable paths |
| [GUI walkthrough](docs/walkthrough.md) | Complete synthetic session and continuation |
| [Protocol and API](docs/protocol.md) | Manual JSON, management RPC, CLI and recovery |
| [Architecture](docs/architecture.md) | Services, state ownership, trust and publication |
| [C#/Inventor](docs/csharp-inventor.md) | SDK, reference assemblies and Windows mappings |
| [Development](docs/development.md) | Build the single VSIX and run verification |

The repository contains Python services, a thin TypeScript extension, static harness files, tests and documentation. Generated runtimes and dependencies stay in ignored build directories. CI also provides a distribution ZIP with the full VSIX, loose .github harness, MCP examples, complete backend source and dependency locks. No separate runtime installation is needed.

Normal indexing, export, requests and backend communication are local. The bundled runtime requires no provisioning. Explicitly authorized commands may restore project dependencies or use the network. Managed .NET commands use `--no-restore` and disable SDK telemetry/background workload updates; arbitrary approved project commands remain outside RepoPilot's network-control boundary.

Private state, recovery data and source exports are excluded locally during setup and from this checkout's Git. All RepoPilot versioned private directories are excluded from source access. No proprietary fixtures or Autodesk binaries are included.

If a portable `.mcp.json` belongs to a separate local-model client, choose **Keep portable config and enable VS Code MCP**. That explicit choice leaves the portable file unchanged and registers the selected project separately with VS Code. Native `.vscode/mcp.json` duplicates still require migration or keeping the existing server.
