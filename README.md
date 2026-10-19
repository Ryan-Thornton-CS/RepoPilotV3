# RepoPilotV3

A local VS Code panel for using a web-only model as a development assistant. Paste its complete reply, review requested actions, and copy the tool response back. RepoPilot tracks sessions and TODOs, prepares source context, and shows native diffs before applying a proposal. It makes **no model API calls**.

Copilot uses a separate **static harness**: four structured agents with native development tools plus RepoPilot's four focused MCP tools. Nothing generates or installs `.github` or MCP configuration into your workspace.

V3 is independent of RepoPilot and RepoPilotV2. There is no state migration or Textual TUI.

## Install

In VS Code, choose **Extensions: Install from VSIX** and select **[repopilotv3-0.2.10.vsix](dist/repopilotv3-0.2.10.vsix)**. Open a trusted source folder and click the RepoPilot icon. That's the entire backend setup.

The VSIX includes **Python 3.12, the backend, pinned dependencies, pytest, C# analysis bindings, documentation, and the static Copilot harness** for **Windows x64 and Linux/WSL x64**. It runs offline without installing Python or downloading dependencies. VS Code **1.85+** is required; Copilot agents/MCP/hooks have separate host requirements. macOS and ARM hosts are not bundled.

SDKs, Framework reference assemblies and Autodesk interop DLLs remain optional host tools for C#/Inventor projects. Copilot harness files are still placed manually through **Open Harness Folder**; the manual panel works immediately without them. See [setup and troubleshooting](docs/setup.md).

The standalone [WebUI project prompt](src/repopilotv3/resources/webui.md) defines behavior, modes and manual tools. Use **⚙ → Copy project prompt** and paste it once into your WebUI project's instructions. It is 7,840 characters with an enforced 8,000-character ceiling, and contains no session identity or repository source.

The normal loop is **New session → copy starter → paste model reply → review changes → copy tool results**.

1. **New session** opens a chat immediately. Paste its short starter into a new chat within your configured WebUI project. It supplies this session's identity/mode and asks for the objective; the model names the chat and creates its task/TODO using the separate project instructions.
2. **Send** prepares your typed message for the WebUI. **Paste + Run** accepts the model’s prose plus tool calls and copies the completed harness reply.
3. Choose Planning, Asking, Implementation or Review below the composer; mode changes create a compact prompt to paste.
4. Source proposals automatically open native diff tabs. **Approve whole proposal** applies the change and runs configured validation. Commands request local permission once per chat and offer **Stop** while running.
5. **Update context**, beside the context meter, shows upload/replace/keep actions. **⚙ → Generate Handoff** continues the task in a confirmed fresh external chat.

The cogwheel holds configuration, validation, command permission and session options. Context tracking is an observed estimate with a configurable 40k advisory threshold, not a measurement of remote model capacity.

Drag the RepoPilot view container or use VS Code's **Move View / Move View Container** actions to put it in the Secondary Side Bar or Panel beside Chat/Codex. It is an independent view, not embedded in another extension.

## What's included

- SQLite/FTS5 incremental search, cited source reads, focused context, cached analysis, and optional Python-hosted Roslyn with labeled text fallback.
- Static Plan, Code, Ask, and Review agents (~4.3–4.7k estimated tokens each), optional handoffs, compact shared instructions, warning-only hooks.
- Persistent searchable/renamable/archiveable sessions, task-specific TODOs, drafts, private transcripts, local notes, rough observed-context estimates.
- Protocol 1.0, complete prose/JSON replies, model task/chat naming, sequential request queues, local approvals and deduplication.
- Permission-bound commands with streaming output, cancellation, durable logs and observed source diffs; advisory modes, standalone project instructions and separate session introductions.
- Automatic size-based export/cache retention, preserving current packages and recovery evidence.
- Complete-file proposals with locally generated diffs, plus legacy exact UTF-8 patches, whole-proposal native diffs, dirty-editor guards, freshness checks, recovery copies and rollback.
- Named local validation profiles, actual JUnit/TRX test evidence, snapshot/configuration provenance, historical-result detection.
- Immutable context generations, unchanged-artifact reuse, focused and optional full-source exports, paste/upload/keep/replace checklist and scoped confirmations.
- A versioned local stdio management API, separate from the four-tool retrieval/checkpoint MCP surface.

## Documentation

| Guide | Purpose |
|---|---|
| [Setup and troubleshooting](docs/setup.md) | VSIX, Windows/WSL, settings and recovery |
| [Static harness](harness/README.md) | Manual placement and MCP executable paths |
| [GUI walkthrough](docs/walkthrough.md) | Complete synthetic session and continuation |
| [Protocol and API](docs/protocol.md) | Manual JSON, management RPC, CLI and recovery |
| [Architecture](docs/architecture.md) | Services, state ownership, trust and publication |
| [C#/Inventor](docs/csharp-inventor.md) | SDK, reference assemblies and Windows mappings |
| [Development](docs/development.md) | Build the single VSIX and run verification |

The repository contains Python services, a thin TypeScript extension, static harness files, tests and documentation. Generated runtimes and dependencies stay in ignored build directories. There are no separate end-user installer scripts or backend archives.

Normal indexing, export, requests and backend communication are local. The bundled runtime requires no provisioning. Explicitly authorized commands may restore project dependencies or use the network. Managed .NET commands use `--no-restore` and disable SDK telemetry/background workload updates; arbitrary approved project commands remain outside RepoPilot's network-control boundary.

Private state, recovery data and source exports are excluded locally during setup and from this checkout's Git. All RepoPilot versioned private directories are excluded from source access. No proprietary fixtures or Autodesk binaries are included.
