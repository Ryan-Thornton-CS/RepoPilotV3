# Architecture and decisions

Session administration runs separately from repository work. Per-session OS leases prevent deletion racing tool execution; nested Python calls share their lease. Deletion publishes a durable hashed marker outside the index database, then purges chat history and attempts a short database cleanup. Busy index writes defer cleanup without keeping the chat visible. Restart completes pending cleanup. Shared task/evidence/recovery records and immutable packages remain intact. Status checks read the current snapshot identifier without loading symbol metadata; unchanged transcript cards retain their DOM nodes during refresh.

## Boundaries

```text
Copilot native tools ── normal edits / terminal / tests
        │
        └── focused MCP ───────────┐
                                 │
VS Code Webview → extension → stdio management RPC
                                 │
CLI ─────────────────────────────┤
                                 ▼
               UI-independent Python services
     repository / tasks / manual / workflow / exports
```

MCP exposes exactly `search_repository`, `read_source`, `get_context`, and `task_checkpoint`. It never executes validation or edits source. Copilot uses native tools for those actions.

Manual protocol 1.0 exposes only registered task/TODO/retrieval/change inspection/patch staging/validation/command/handoff operations. Trusted local management additionally owns session administration, approval/recovery, transfers and history. A model cannot call those administrative operations by naming a management method inside a manual request.

The extension is TypeScript/HTML/CSS because VS Code's stable extension API runs in that environment. Repository intelligence and workflow logic remain Python. There is no C# implementation worker, Textual TUI, Python clipboard integration, model client, browser automation or harness generator.

## Source and task ownership

`repository/index.py` captures UTF-8 source bytes twice around analysis and records hashes, Git HEAD, analysis configuration, and available Roslyn document checksums. Inconsistent capture retries/fails rather than publishing mixed versions. SQLite/FTS5 indexes content-addressed files incrementally. Unchanged semantic inputs reuse analysis; all source-serving/patch operations refresh first.

`repository/service.py` supplies shared bounded search/source/context and checkpoint behavior to MCP and manual dispatch. Relationships are semantic when Python-hosted Roslyn resolves them; text-only fallback is explicitly labeled. Dynamic runtime relationships are not claimed complete.

Search currently uses local FTS5 keywords and available compiler relationships, without embedding API requests or a Copilot sign-in. Compiler semantic analysis is distinct from embedding similarity search. A future opt-in embedding provider could add ranked vector candidates alongside keyword matches, caching vectors by source chunk hash and provider/model version and embedding only changed chunks. Initial indexing and uncached queries would consume embedding input tokens; retrieval output would still consume reasoning-model context. No embedding provider or automatic API expenditure is enabled by this design note.

The independent hook monitor reads Copilot failure `error` fields, keeps outcomes separate in fingerprints, and deduplicates tool-call IDs without using changing timestamps. Repeated native failures produce advisory guidance without claiming unchanged source. For Copilot command failure hooks that guidance uses stdout/exit 2; successful hooks and expected monitoring failures use JSON/exit 0. Pre-tool/stop handlers never request denial or another turn. Missing bindings, opaque results and ambiguous correlation retain partial coverage; the handler does not index source. Native Local hook output compatibility and actual installed-Copilot delivery require separate host verification.

`tasks/store.py` owns `task-state.json`. Stable items and merged unresolved lists preserve requirements. `05_TODO.md` is a rendering. Model/developer progress is labeled unverified. Only local validation execution creates evidence; native Copilot assertions cannot forge it.

## Private storage

```text
.repopilotv3/
  config.json                  # host/root ownership, SDK, locally approved profiles
  index.sqlite3                # snapshots, source content, tasks, sessions, request dedup
  history.sqlite3              # full transcript entries, drafts, selected session
  monitor.sqlite3           # fingerprint metrics and advisory warnings
  tasks/<task>/task-state.json
  patches/<patch>/             # proposal, recovery originals, journal
  validation/<result>/         # exact process result and test reports
  outbox/<chat>/               # copyable saved JSON responses
  exports/
    generation-*/              # immutable complete package
    artifacts/                 # unchanged content reuse
    current-<chat>.json         # atomically replaced published pointer
    signature-<chat>.json       # cheap unchanged-package reuse across reconnects
```

The monitoring filename is an implementation detail; use diagnostics to inspect capabilities. Private SQLite connections use short WAL transactions. Source jobs use an OS-released repository writer lock. The JSON-RPC server serializes source work through one executor while history/status reads remain independent. A slow build does not monopolize the stdio reader.

No global active-task pointer binds requests. A selected-session preference controls presentation only; every action carries its explicit session ID, and every RPC frame carries its workspace root. Fresh chats can share a task but have independent TODO readiness, drafts, request IDs and chat receipts.

## Protocol and interruption

The backend uses newline-delimited JSON-RPC 2.0 on local stdio, with `bridge_version: "1.0"` inside parameters. MCP uses its own documented local transport. They are separate entry points. Output frames are synchronized; progress notifications identify jobs; diagnostics go to stderr.

Manual requests reserve `(session, request_id, fingerprint)` before dispatch. Exact duplicate IDs return the existing result; conflicting reuse fails; a reservation without a result remains interrupted and is never replayed. Full local request/result history is stored privately, separate from observed-transfer estimates.

Panel disposal/restart restores passive history and drafts. Connection loss rejects pending calls. Reconnect is explicit and reads state only. Patch journals and interrupted request warnings tell the developer what needs inspection. Administrative operations have no automatic retry guarantee: inspect local state after uncertain outcomes.

## Diff, application and evidence

Original/proposed text is supplied through a read-only custom URI scheme to VS Code's native diff editor. Approval is for the entire proposal. Dirty affected documents block application; explicit save/refresh can invalidate the old proposal, requiring a new one. The backend rechecks the snapshot and all affected hashes independently.

Patch application preflights exact hunks, preserves text newline conventions, retains originals/modes, journals outputs, and rolls back write failures. Later editor changes are never overwritten by recovery. Multi-file edits are rollback-protected local writes; they are not a filesystem-wide transaction visible atomically to unrelated processes.

After successful application, a locally selected named profile runs automatically when configured. Source application and validation outcomes remain separate. Without a configured profile, the result explicitly says validation was not run. Profiles use argument arrays; model JSON supplies only their names. JUnit/TRX evidence distinguishes actual tests from successful checks. Changed source/configuration makes old results historical.

## Context generations and estimates

Each generation is immutable and becomes current only after complete artifact writes and a matching final source capture. Unchanged artifact bytes retain creation metadata; manifest `verified_snapshot` describes the current verification. Dependency checks permit reuse without forcing re-upload after unrelated changes.

Active-session packages refresh by periodic source capture; file notifications are available through the shared watcher, and polling is the correctness fallback. Inactive packages are shown stale and refresh on resumption. Optional full-source Markdown exports split by complete logical files. Oversized/omitted content is explicit; bounded tools can expand it.

Receipts are scoped to named external projects for project guidance/maps and to chats for task packets. Generation never confirms transfer. Local copy is measured separately from certainty of remote paste. Repeated copy/confirmation of the same artifact is deduplicated within a chat.

Observed token estimates use `ceil(max(characters / 3.2, UTF-8 bytes / 4))`, with low confidence. Unrecorded remote prose, hidden model truncation, retrieval and caching are unknown. Repetition/checkpoint/handoff warnings are advisory; there is no forced conversation-length cutoff or claimed model billing savings.

## Trust and packaging

No backend starts before Workspace Trust. Webview messages use a closed action set; backend parameters use closed schemas and explicit scope. Data uses `textContent`/plain JSON with a restrictive CSP. Source links reject traversal/URI/private paths and resolve within the workspace.

Static harness assets are authored under `harness/`, included unchanged in wheel/VSIX, and manually merged by the developer. Setup touches only private state and local Git exclusions. The VSIX carries locked Windows x64 and Linux x64 Python runtimes and installed dependencies. Only an explicit developer build provisions them; extension execution performs no environment creation or downloads. No V2-state migration, network listener, external publication or automatic commit occurs.

## Chat harness and command supervision

PromptService generates bounded starters, user-forward prompts, mode deltas and deduplicated context advisories. ReplyService validates complete pasted replies before execution and journals sequential tool progress and local decision pauses. Each incoming reply is one observed event; underlying tool calls retain independent IDs and monitoring. Task revisions retain prior objectives/checklist wording; local validation evidence remains independent.

ProjectInstructions renders reusable project behavior independently, with an 8,000-character ceiling, checked for both copied and exported instructions. Fresh-chat starters contain only the project/session introduction and mode; they do not index source or embed the project prompt, tool catalog or map. Continuation starters add current task progress and evidence. Old combined cached starters are replaced on the next explicit starter request; passive history restoration does not rewrite them. Export signatures include instruction/protocol/schema fingerprints, so prompt revisions publish new instruction artifacts and require replacement even without source edits.

The UI is a transcript and composer with mode/TODO/context/Update context in its bottom bar. Session creation is immediate and blank; the model owns naming and task initialization. All settings/administrative actions remain outside the model registry. Foreground action generations and explicit chat binding prevent late results or background refresh from clobbering the clipboard.

ConversationTurns groups saved events by user/model/harness role, with explicit incoming-message and settings boundaries. The webview renders one Copy per turn and reuses expanded event DOM nodes during refresh. The local history.copy_turn method walks adjacent saved entries around an anchor, independently of page boundaries, and copies all results/errors and continuation metadata without execution. Identical results are deduplicated; user copies use their prepared forward prompt and model copies retain raw JSON requests. Per-tool limits still apply, while aggregate replies retain every returned operation instead of applying another truncation budget. Reply parsing accepts adjacent objects or request arrays within one fence, validates the entire message before effects, and enforces the eight-request and unique-ID limits globally.

Panel-backed workflows mark routine package work dirty after a reply instead of waiting for exports before returning it. The panel displays saved results before clipboard/diff work and refreshes dirty active-task packages afterward/in its polling loop. Blank chats do not trigger automatic indexing; Update context still explicitly generates a package. CLI requests retain synchronous refresh. Every source read and patch operation retains its source-freshness checks; deferral concerns upload packages, not repository verification. Dirty flags persist across restart and are cleared only after successful generation/reuse. Local Output logs record backend/panel startup, requests/handlers over 500 ms and slow clipboard operations, without logging prompt bodies.

CommandRunner runs argv only after a persisted local session grant. A separate Python supervisor watches its backend-owned stdin pipe; EOF cancels the actual process tree even if the backend is killed. Linux uses a process group; Windows assigns the supervisor to a kill-on-close job before launching descendants. Commands have closed stdin and bounded timeouts. Full logs and before/after eligible source observations stay under private commands directories. Such observations can include concurrent external edits and are not a sandbox or complete side-effect inventory.

The management API can inspect status/history and cancel commands while serialized repository work runs. Permission/settings cannot be changed by naming their management methods in a model request. New chat continuations never inherit command grants. Generic command exit status is recorded honestly, without manufacturing test counts. Context/evidence and continuation packets carry compact task-bound command facts, freshness and originating-chat IDs; complete command logs remain private to their original chats. Read-only commands also invalidate the evidence-package cache when new facts are recorded.

Model-facing Copy renders a short summary and fenced JSON with `kind: "tool_results"`. `history.copy_turn` also returns the parsed `copy_data` object. It consolidates the latest task once, removes duplicate Markdown TODOs and internal monitoring/timestamps, and preserves source text, citations, omissions, warnings, failures and validation evidence. Full original events remain in private history. Replaying a result envelope as a request produces `response_pasted` without executing tools. Keyword search includes `indexed_file_count`; `search "*"` lists bounded eligible paths and hashes.

`manual/content_edits.py` generates exact unified diffs from full-file proposals. `PatchManager` checks snapshot, source policy and base hashes before generating an immutable staged diff. It then reuses the existing exact review/application/recovery path; apply never regenerates from newer source. Mixed unchanged files are omitted with explicit metadata. Full replacements require complete source, while legacy bounded unified diffs remain available for large-file edits. Both forms keep manual protocol 1.0 and leave the MCP surface unchanged.

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
