# Protocol, management API and CLI

## Model-facing manual protocol 1.0

Paste + Run accepts complete model replies with prose and up to eight requests. A JSON fence may contain one object, multiple adjacent objects (including JSON Lines), or an array of request objects. The same object streams/arrays work without prose/fences. All requests across all blocks validate before any execute; malformed later objects prevent earlier effects. Duplicate IDs and the eight-request limit apply to the entire reply:

```json
{"protocol_version":"1.0","request_id":"start-1","tool":"todo","arguments":{"progress":{"items":[{"id":"fix","text":"Fix greeting and verify","status":"active"}]}}}}
```

Use task to create/resume the objective and set chat_name, then todo to initialize the checklist. The legacy combined todo objective/progress form still works. Once bound, the selected session injects its task. **Each new manual chat must begin with `todo`.** Reusing a task does not inherit the old chat's readiness.

| Tool | Arguments |
|---|---|
| `task` | optional objective/task_id/seeds/chat_name; create/resume/refine objective and chat name |
| `run_command` | argv, optional cwd and timeout (1–3600 seconds, default 300); local session permission required |
| `command_output` | command_id, optional start/end; 500 lines/12,000 characters maximum |
| `todo` | optional objective/task_id/seeds/progress |
| `search` | query, optional limit |
| `read` | path, optional start/end (at most 500 lines) |
| `context` | optional query/paths |
| `changes` | optional validation_id/start/end for bounded evidence |
| `propose_patch` | base_snapshot, base_hashes, exactly one of files (complete contents/null deletion) or diff |
| `validate` | configured profile name only |
| `handoff` | empty object; saves current packet, does not register another chat |
| `batch` | 1–8 search/read/context/changes/command_output calls only |

Published schemas: [`request.schema.json`](../src/repopilotv3/resources/schemas/v1/request.schema.json), [`progress.schema.json`](../src/repopilotv3/resources/schemas/v1/progress.schema.json). The complete generated protocol examples are in [`resources/protocol.md`](../src/repopilotv3/resources/protocol.md); packages include `07_PROTOCOL_REFERENCE.md` and the request schema.

Responses contain protocol version, request/session/task identity when available, `ok`, and `data` plus advisory monitoring or a structured `error`. Source results carry snapshot/hash/path/line citations and explicit omissions. Failed parsing also produces saved, copyable output. Do not invent successful results or execute shell text from a model.

Progress fields: stable `items` (id/text/status), errors, files, decisions, questions, assumptions, required_tests. Statuses: pending/active/blocked/completed. Lists merge without silently deleting unresolved state. Rewording an item retains its previous revision; omitted requirements remain unresolved. Validation facts cannot be supplied in progress.

Exact duplicate requests return the saved response without effects or additional exchange counts. Conflicting IDs fail. Reserved interrupted requests require state inspection and a new ID for genuinely new work. A read-only batch is one message, with separate tool-work observations for its executed operations.

## Patch workflow

1. Retrieve current source/snapshot and hashes.
2. `propose_patch` preferably accepts `files`, mapping paths to complete UTF-8 contents (null deletes; empty string creates/empties). RepoPilot generates the staged diff. `base_hashes` covers all supplied paths, with null for additions. Legacy exact `diff` remains supported; supply exactly one format.
3. Inspect the automatically opened native diff tab for every proposed file (read-only original/proposed documents).
4. **Approve whole proposal** applies all files; **Reject** changes none. There is no hunk/file acceptance.
5. Dirty affected editors block application. Explicit save/refresh may stale the proposal; request a new one from current source.
6. Backend freshness checks and recovery journaling run independently of the editor guard. Selected validation follows successful application.

Approval/recovery/session administration are **not** callable through model JSON or MCP. No arbitrary shell operation exists. Traversal, private paths, symlinks, binary patches, renames/modes, malformed hunks and partial applicability fail closed.

## Local management JSON-RPC 1.0

Launch with an argument array:

```bash
python -m repopilotv3 --repo /absolute/source bridge serve
```

Frames are UTF-8 JSON, one per line; this is JSON-RPC **2.0**, with management protocol **1.0** parameters. Maximum input frame is 8 MiB. Every call requires an ID, exact workspace root and bridge version:

```json
{"jsonrpc":"2.0","id":1,"method":"hello","params":{"bridge_version":"1.0","workspace":"/absolute/source"}}
```

`hello` returns backend version, bridge version, initialization state and supported method names. The extension requires protocol 1.0 and backend 0.2.x. Errors use JSON-RPC error objects with stable local codes in `error.data`; uncertain failures never authorize replay. Slow work emits `progress` notifications with token/request/method/state. No TCP listener or model connection exists.

| Methods | Additional parameters |
|---|---|
| `hello`, `diagnostics`, `sessions.selected`, `tasks.list` | none |
| `harness.preview` | optional repository_root, repository_state, server_name; local administration only |
| `harness.apply`, `harness.recover` | plan_id or recovery_id respectively |
| `harness.status` | none; generation/runtime drift and interrupted updates |
| `setup` | optional validation enum: keep/python/dotnet_test/dotnet_build |
| `sessions.list` | optional query, archived |
| `sessions.create` | optional name, objective **or** task_id, project |
| `sessions.select`, `sessions.status` | session_id |
| `sessions.update` | session_id, optional name/archived |
| `sessions.delete` | session_id; local administration only, retains shared tasks/evidence/recovery |
| `history.list` | session_id, optional before sequence, limit 1–100; byte-bounded pages |
| `history.copy_turn` | session_id, entry_id; complete saved turn containing the anchor, independent of displayed history pages |
| `draft.save`, `notes.add` | session_id, text |
| `todo.update` | session_id, progress; developer-authored/unverified |
| `prompts.starter`, `prompts.compose` | session_id; compose additionally takes user text; prepares copy-ready prompts |
| `prompts.project` | no session/task required; standalone reusable instructions, revision, fingerprint and an 8,000-character ceiling (`max_characters`; token estimates remain separate) |
| `sessions.configure` | session_id, optional mode/context_limit/commands_allowed/project; local administration only |
| `replies.preview`, `replies.import` | session_id, text; preview validates without effects, import runs the complete reply |
| `replies.status` | session_id, reply_id; passive pending-state inspection |
| `replies.resolve` | session_id, reply_id, approved, optional profile; local approval only |
| `commands.cancel` | session_id, command_id; responsive cancellation during source jobs |
| `commands.diffs` | session_id, command_id, optional path; metadata or complete observed source text |
| `manual.execute` | session_id, text; protocol 1.0 parser/registry |
| `patch.review` | session_id, patch_id, optional path; without path returns file metadata, with path complete original/proposed text |
| `patch.recover` | session_id, patch_id |
| `patch.approve` | session_id, patch_id, approved, optional approved profile name |
| `exports.prepare` | session_id, optional full_source/background; background returns deferred when another writer holds the repository |
| `transfers.confirm` | session_id, generation_id, confirmations mapping filename→exact hash |
| `transfers.record` | session_id, text; local copy observation only |
| `handoff` | session_id, confirmed_new_chat; false prepares only, true registers replacement binding |

All objects are closed; unknown methods/extra arguments/wrong versions/wrong roots fail. Mutation callers must inspect state after a dropped connection rather than automatically retry. Metadata/history remains responsive during serialized repository work. Published management request schema: [`bridge.schema.json`](../src/repopilotv3/resources/schemas/v1/bridge.schema.json). It complements runtime checks against the exact workspace root.

The exported [server method schemas](../src/repopilotv3/rpc/server.py) are authoritative.

## CLI

```bash
repopilotv3 --repo /source setup --validation python
repopilotv3 --repo /source doctor
repopilotv3 --repo /source session create --objective "Fix greeting" --name "Greeting"
repopilotv3 --repo /source session list
repopilotv3 --repo /source session configure --session chat-ID --mode code --context-limit 40000
repopilotv3 --repo /source session configure --session chat-ID --allow-commands
repopilotv3 --repo /source request model-reply.md --reply --session chat-ID
repopilotv3 --repo /source request request.json --session chat-ID
repopilotv3 --repo /source patch review patch-ID --session chat-ID
repopilotv3 --repo /source patch apply patch-ID --session chat-ID --confirm-reviewed
repopilotv3 --repo /source patch recover patch-ID --session chat-ID
repopilotv3 --repo /source export --session chat-ID --full-source
repopilotv3 --repo /source mcp serve
repopilotv3 --repo /source bridge serve
```

`--state /absolute/private/path` selects host-owned external state. No CLI TUI/open command or harness installer is present. Setup initializes configuration/validation only. Patch apply is explicit and runs the selected default profile if configured. Recovery never repeats validation or overwrites newer edits.

## Commands, pending replies and context

Command permission is granted/revoked through local management or CLI configuration, never model JSON. Commands use argv arrays with configured Python/dotnet resolution. They are noninteractive and may write/restore/network under that grant; no implicit shell is inserted. A pipe-watching supervisor kills descendants on cancellation, timeout or backend loss; Windows uses a kill-on-close job and Linux process groups. Full logs are private. Generic command results are distinct from JUnit/TRX test evidence. Compact command facts follow the task into handoffs, including source freshness and originating chat; full logs remain scoped to their original session.

A reply journals processing, awaiting_permission/awaiting_approval, completed, failed or rejected. Local resolution runs a staged command/application once and can advance only unexecuted later requests. Errors/declines stop the queue. Reconnect never resumes uncertain processing. Results correlate request IDs. Individual tools remain bounded with explicit omissions; combining a turn does not truncate those results again. Its single Copy action includes saved results, errors, local outcomes and remaining request IDs, deduplicating identical results. The complete copy can exceed an individual tool's response budget.

Context counts unique recorded transfers and complete model replies. At the session’s configured threshold (40,000 estimated tokens by default), one advisory is appended to the next copy-ready tool reply and a packet is prepared. Work remains allowed. Copying is observed content, not proof of remote paste. New sessions have no active role mode; continuations retain the task and reset TODO readiness and command permission.

Model-facing Copy renders a short summary and fenced JSON with `kind: "tool_results"`. `history.copy_turn` also returns the parsed `copy_data` object. It consolidates the latest task once, removes duplicate Markdown TODOs and internal monitoring/timestamps, and preserves source text, citations, omissions, warnings, failures and validation evidence. Full original events remain in private history. Replaying a result envelope as a request produces `response_pasted` without executing tools. Keyword search includes `indexed_file_count`; `search "*"` lists bounded eligible paths and hashes.

## MCP repository setup and local harness administration

MCP retains the four retrieval/checkpoint tools and adds `setup_repository(repository_path?)` and `repository_status(repository_id?)`. All four existing tools accept an optional repository_id. Omission always means the startup root. Setup accepts only existing directories within that root and returns a stable opaque repository_id; it performs no validation, dependencies or harness writes. Status works before initialization. Bind subprojects again after a server restart. Source tools report not_initialized with setup guidance rather than preventing server discovery.

Management RPC adds `harness.preview` (optional repository_root, repository_state, server_name), `harness.apply` (plan_id), `harness.status`, and `harness.recover` (recovery_id). These are trusted local administration, unavailable to model JSON/MCP. Overrides must keep the repository inside the target workspace root. Preview returns concrete original/proposed files, actions, conflicts and a plan ID; apply refuses conflicts, changed bases or old-runtime previews. Recovery uses its durable journal and refuses later edits. Setup still never generates a harness automatically.

## Composer, prompt and checklist administration

`composer.preview(session_id,text)` returns `route` (`reply` or `message`) and `has_commands` without effects. `composer.submit(session_id,text)` uses the same classification: request-shaped text enters complete-reply import, ordinary prose enters prompt composition. Malformed request-shaped text stays a durable import error. The editor checks dirty buffers before submissions containing commands. `prompts.compose` remains the explicit no-execution forwarding override; CLI protocol 1.0 is unchanged.

`prompts.read(role)` returns exact UTF-8 Markdown, path, content hash, customization flag and default version. Roles are planning/asking/implementation/review. Missing files initialize from shipped defaults in private state. `prompts.reset(role,expected_hash)` performs a compare-before-write reset and retains previous bytes with metadata in private recovery storage. The editor confirms resets and blocks unsaved prompt-buffer conflicts. These methods do not require or create a session; copying optionally records an outgoing transfer in the selected chat.

`todo.reset(session_id,expected_revision)` is a local, confirmed administrative operation. It archives checklist items, retains validation/requirements, invalidates readiness across chats sharing the task and returns a copy-ready recovery message. Concurrent task changes produce `stale_task`; changed prompts produce `stale_prompt`. Neither reset is model-callable.

Legacy saved mode fields and CLI configuration arguments remain readable, but no longer affect starters, outgoing prompts or handoffs. Role prompts copy their exact file contents and change no session state or permissions. Project-list persistence belongs to the extension and uses stable folder identifiers; backend operations remain root/session-bound.
