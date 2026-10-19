# Complete chat walkthrough

This uses disposable Python source and real local pytest evidence. The WebUI is the model; RepoPilot is its local tool harness. No model API, account connection or browser automation is involved.

## Configure the project, then open a chat

For a synthetic project, a source developer can run:

```bash
uv run --frozen python examples/create_demo.py /tmp/repopilotv3-demo
```

Alternatively create `greeter.py` with a small function and a failing test in a disposable folder. Install the single VSIX, open that folder in a trusted VS Code window, and open RepoPilot.

If your VS Code workspace contains multiple projects, choose **⚙ → Select workspace** and browse to the one project you want RepoPilot to use. Its path appears in the options menu and is remembered after reload. Other folders remain open in VS Code. Hover a control to see its short purpose.

Use **⚙ → Copy project prompt** and paste the result into your WebUI project's instructions once. This standalone document explains behavior, modes, TODOs, the JSON tools, approval and evidence rules. It stays below 8,000 characters. Repository maps and source remain separate context files.

Click **New session**. It opens a chat and copies a short starter. Paste that into a new external chat within the configured WebUI project. It introduces this session and mode, then asks for the objective. It does not repeat the project instructions, protocol catalog or source map. No separate task/name/project dialogs are required.

The message input grows upward as text wraps, up to 30% of the panel height, then scrolls internally. It shrinks again when you shorten or send the message and resizes when the panel width changes.

Write your goal in the WebUI, or type it into RepoPilot and choose **Send**, then paste the prepared prompt there: “Fix greeting punctuation without changing the Greeter interface.” Send prepares text; it does not execute typed commands.

The model can answer with prose and two JSON blocks:

```json
{"protocol_version":"1.0","request_id":"task-1","tool":"task","arguments":{"objective":"Fix greeting punctuation without changing the public API","chat_name":"Greeting punctuation","seeds":["greeter.py"]}}
```
```json
{"protocol_version":"1.0","request_id":"todo-1","tool":"todo","arguments":{"progress":{"items":[{"id":"inspect","text":"Inspect greeting and regression test","status":"active"},{"id":"fix","text":"Correct punctuation and verify tests","status":"pending"}]}}}}
```

Copy the entire model reply and click **Paste + Run**. The model may also put both objects into one JSON fence, separated by a newline, or send an array of requests. The chat gets its name, the task becomes persistent and the TODO appears in the bottom bar. One pasted reply is one recorded conversation message, even with multiple tools. Tool calls, results and status messages appear together under one harness turn with one **Copy** button. It copies the complete saved turn, including results outside the displayed history page. Automatic copying uses the same complete turn; paste it back into the WebUI. Expand individual results for details without having to copy each fragment.

Open **☰ Sessions** to switch chats. Each row has **Archive** and **× Delete** controls. Archive hides the chat; enable **Show archived** and choose **Restore** to bring it back. Delete asks for confirmation and removes that chat's transcript, draft and conversation counts. Shared tasks, validation/command evidence, immutable packages and recovery files remain. The same controls are available under **⚙** for the selected chat. Deleting the last chat returns to the empty view; creating another chat never replays the deleted requests.

## Change modes and retrieve only needed source

New independent sessions start in **Planning**. Choose **Implementation** in the bottom bar when ready. RepoPilot emits “Mode changed to Implementation” with a compact prompt and copies it. Paste that into the WebUI. Asking and Review work the same way; modes guide the model without changing permissions or replacing the task.

The model can retrieve bounded source:

```json
{"protocol_version":"1.0","request_id":"read-1","tool":"batch","arguments":{"calls":[{"tool":"read","arguments":{"path":"greeter.py","start":1,"end":40}},{"tool":"read","arguments":{"path":"test_greeter.py","start":1,"end":40}}]}}
```

Paste + Run, then paste the response back. It includes source ranges, hashes and snapshot metadata. The model should reuse these results until the source changes.

## Inspect and approve changes

The model returns `propose_patch` with the **actual snapshot and hash** from retrieval and complete proposed file contents. Replace the illustrative snapshot/hash below with returned values:

```json
{"protocol_version":"1.0","request_id":"patch-1","tool":"propose_patch","arguments":{"base_snapshot":"ACTUAL_SNAPSHOT","base_hashes":{"greeter.py":"ACTUAL_HASH"},"files":{"greeter.py":"\"\"\"Small synthetic greeting module.\"\"\"\n\n\nclass Greeter:\n    \"\"\"Build a greeting for one person.\"\"\"\n\n    def greet(self, name: str) -> str:\n        \"\"\"Return the current greeting behavior.\"\"\"\n        return f\"Hello, {name}!\"\n"}}}
```

RepoPilot generates and counts the diff locally. `files` maps paths to complete contents, `null` deletes, and `""` creates/empties a file. Additions use `null` base hashes; existing files require their current hashes. Never send a partial file as replacement contents. Legacy unified `diff` proposals remain available for bounded edits in large files; do not send both formats.

Paste + Run automatically opens a permanent native diff tab for every proposed file. Source is unchanged. Inspect the read-only original/proposed documents and choose **Approve whole proposal** in the chat. No file-picker step is needed. Dirty affected buffers or changed source prevent application.

To run actual tests after approval, select **⚙ → Validation profile → python** beforehand. This approves the bundled pytest profile locally; dependencies are already present. Approval applies the complete proposal, runs that configured profile, and returns actual evidence. No profile means validation is explicitly not run. A failing profile does not undo an applied source change.

## Run a command when needed

The model can request:

```json
{"protocol_version":"1.0","request_id":"check-1","tool":"run_command","arguments":{"argv":["python","-m","pytest","-q"],"timeout":300}}
```

For C# use argv such as `["dotnet","build","--no-restore"]`; SDK configuration chooses the executable. On first use, the chat displays the exact command and working directory. Choose **Allow commands for this session** to execute it and authorize subsequent commands. This permission persists for this chat and can be revoked from ⚙.

Commands run with your account permissions, can modify files/use the network, and are noninteractive. Ordinary source edits should still use reviewed patches. RepoPilot asks you to save or cancel dirty workspace source buffers before execution. Live output appears in the transcript; **Stop** cancels the command tree. Eligible source changes observed during execution open native diff tabs afterward.

Complete logs stay private. The model can use command_output with the returned command ID and bounded line ranges. Generic command exit status is actual execution evidence but does not establish that tests ran; the validate tool/profile records genuine JUnit/TRX test counts.

## Refresh WebUI files from the message bar

The bottom bar contains:

`Implementation ▾   TODO 1/2   Context ~18k/40k   Update context · 3`

Source/task changes refresh packages automatically. **Update context** refreshes immediately and opens the upload checklist. Use **Open folder** and upload/replace your chosen files. Compact packages contain repository maps, protocol/schema reference, focused task source, TODO and validation evidence. Optional full-source Markdown is selected through ⚙.

Use **Copy instructions** or **⚙ → Copy project prompt** for the reusable project guidance. Session starters rely on those separately configured instructions. Local copying does not prove a remote paste or replace attachments. Choose **Confirm transfers** only after completing the exact external uploads/pastes. KEEP means confirmed content is still valid; UPLOAD/PASTE/REPLACE identify required action. A revised project prompt is flagged for replacement even when source files are unchanged.

## Continue, recover and correct errors

The model updates todo as work proceeds. Local build/test evidence remains independent from model-reported completion. At the default 40,000 observed estimated tokens, RepoPilot adds an advisory to the next tool reply and prepares a handoff packet; it does not block work. Change the threshold through the context meter or ⚙. The estimate cannot measure the WebUI's hidden retrieval, summaries or actual remaining capacity.

Choose **⚙ → Generate Handoff**, open a fresh external chat in the configured project and confirm that it is open. The replacement session keeps the task/mode, gets its own identifier and continuation starter, and requires a new initial todo call and command grant. Its starter carries progress and evidence without repeating the project prompt. Copy/paste it and continue.

For malformed JSON, missing executables or stale source, RepoPilot provides a copy-ready cause and corrective next step. Paste it back so the model can repair its next call with a **new request ID**. All tool blocks validate before the first executes. Identical requests return saved results; conflicting ID reuse is rejected.

After a disconnect, **Reconnect backend** restores passive history and drafts. Nothing is replayed. Interrupted effects must be inspected before another action. Use **⚙ → Local recovery checkpoint** when the previous model is unavailable. Source recovery remains an explicit CLI/management operation and refuses to overwrite later edits.

Copilot fallback uses the same task checkpoint: manually place the static harness, save a task with task_checkpoint during API work, then resume its task ID in the WebUI task tool or the CLI's session create --task option. MCP remains retrieval/checkpoints; Copilot uses native editing, terminal and test tools.

Harness Copy now provides a short summary followed by fenced `tool_results` JSON. Internal counters/timestamps and duplicate task/TODO presentations are omitted; source, hashes, errors, local validation and pending work remain. Paste this **into the WebUI model**, then return its next requests to RepoPilot. Blue message backgrounds distinguish the developer, model and harness; the selected session has a blue marker.

`search` is keyword search: use short identifiers. Use `query: "*"` for a bounded eligible-file list. `indexed_file_count: 0` means no eligible source was found; an empty `hits` list alone only means no matches. An intended empty project can proceed with additions using the returned snapshot and null base hashes.

Generated export folders/cache are cleaned automatically once their combined size exceeds 512 MiB. Current and recent packages remain protected; use **Open folder** for the current generation rather than bookmarking an older upload directory. Cleanup never updates the external WebUI or deletes task/validation/recovery records.
