# Static Copilot workspace harness

`workspace/` is authoritative. The VSIX ships these exact files, unchanged. **Workspace generation is optional and requires an explicit local preview/apply action.** Plain CLI setup and MCP repository setup still initialize private state only. The packaged source bundle remains unchanged.

## Recommended extension setup

1. Choose **⚙ → Project & execution → Select workspace** and select the individual project.
2. Choose **Copilot & MCP → Enable MCP for selected project**. The bundled interpreter is used; no PATH edits or separate Python install are needed. VS Code retains its trust/start/tool-picker controls.
3. Existing RepoPilot entries are detected in the project and enclosing workspace. Keep them or explicitly switch after reviewing the exact MCP configuration diffs. Unrelated JSONC comments/entries are preserved.
4. Choose **Generate / update workspace harness**. Default to the selected project; an enclosing workspace root is offered when applicable. Inspect the proposed agent/instruction/hook diffs and apply them locally.
5. Select Plan, Code, Ask or Review in Copilot. Generated agents reference the project-specific MCP server name. Open **MCP status** to see exact registered roots and inspect actual connection state through VS Code's **MCP: List Servers** command.

Native-provider registration does not write an MCP file. Older hosts with native MCP but no provider API can preview a `.vscode/mcp.json` fallback. Hosts with no MCP support retain the manual panel and report the missing capability. File-based registrations/quoted hook commands can need an explicit refresh after an extension upgrade; generator status flags stale runtime paths. Existing processes never change repository roots when the panel switches projects.

Generated files use private ownership hashes. Existing project instructions are retained around a clearly marked block. Unowned/modified agents or hooks are conflicts, not overwritten files. Review and merge your changes deliberately; requesting a new preview rechecks the current source. Failed generation uses private recovery copies under `.repopilotv3/harness/recovery`; **Recover harness update** refuses to overwrite later developer edits. The static files below remain an alternative.

## Manual placement

1. Open **RepoPilotV3: Open Harness Folder**, or open this directory in the source checkout.
2. Read your existing workspace instructions/MCP entries first.
3. Manually copy the four files from `workspace/.github/agents/` into your workspace's `.github/agents/`.
4. Manually merge `workspace/.github/copilot-instructions.md` into your existing instructions. Keep only the RepoPilotV3-specific language/docstring paragraph when working on RepoPilot itself; adapt project conventions for other repositories.
5. Choose **one** MCP format below and manually merge its `repopilotv3` entry. Preserve unrelated entries.
6. Optionally copy `workspace/.github/hooks/repopilotv3.json` if your installed Copilot runtime supports that format/events. Check runtime logs after the first session.
7. Start MCP and call setup_repository, or run `python -m repopilotv3 --repo /absolute/repository setup`. Select an agent explicitly in Copilot.

No automatic upgrade rewrites your workspace. Generated files update only through the explicit preview flow; manually placed files remain yours.

## MCP discovery and Python paths

`workspace/.mcp.json` uses the portable `mcpServers` structure. Some compatible editors consume it directly; **do not assume every VS Code release discovers root `.mcp.json`**. For native VS Code MCP, manually merge `alternatives/.vscode/mcp.json`, whose top-level key is `servers`. Do not register both formats if your editor would launch two servers.

The default command is `python` with an argument array. It must resolve to an environment with RepoPilotV3 installed. The panel itself uses bundled Python. To reuse it for MCP/hooks, find the installed extension folder through **Developer: Open Extensions Folder** and choose `backend/win32-x64/python/python.exe` or `backend/linux-x64/python/bin/python3.12`. Update this absolute path when the extension moves/updates. Activating a terminal environment alone may not change the editor process's PATH. If needed, edit the copied MCP entry to an absolute executable:

```json
{"servers":{"repopilotv3":{"type":"stdio","command":"C:/Users/ryan/venvs/repopilotv3/Scripts/python.exe","args":["-m","repopilotv3","--repo","${workspaceFolder}","mcp","serve"]}}}
```

For portable root `.mcp.json`, use a concrete absolute root in `--repo` if the host does not start the process in the workspace. `${workspaceFolder}` expansion is a host feature, not part of the backend. Keep executable and arguments separate; never put a command line in `command`.

The panel's `repopilotv3.pythonPath` setting configures **only the panel backend**. It does not rewrite this static MCP or hook configuration.

## Agents

- **Plan:** requirements, architecture, decisions and acceptance criteria; read/search/checkpoints.
- **Code:** focused implementation and validation; native read/search/edit/execute plus MCP.
- **Ask:** source-grounded explanations and bounded investigation; read/search/checkpoints.
- **Review:** concrete defects, regressions and evidence; read/search/execute plus MCP.

Each contains substantial structured guidance and examples within approximately 5,000 locally estimated tokens. That allowance is a ceiling for useful instructions, not a request for verbose answers. Handoff buttons have `send: false`: the user chooses the transition. No coordinator, automatic delegation, model override or permission bypass is configured.

Native tool-set names can vary by installed Copilot version. Use that runtime's agent editor/tool picker to verify `read`, `search`, `edit`, `execute`, and `repopilotv3/*`. Missing tools should be reported or manually adjusted, not replaced with custom MCP execution.

## Warning-only hooks

Commands are static `python -m repopilotv3.integrations.hooks --repo . --event EVENT`. Payloads arrive through stdin; no payload fields are interpolated into shell commands. If Python is not on PATH, manually change the copied commands:

- Bash: `'/home/ryan/venvs/pilot/bin/python' -m repopilotv3.integrations.hooks --repo . --event postToolUse`
- PowerShell: `& 'C:/User Tools/Pilot/Scripts/python.exe' -m repopilotv3.integrations.hooks --repo . --event postToolUse`

Hook event names/output contracts differ between Copilot-compatible and native Local runtimes. The supplied file uses the Copilot-compatible `version: 1` format. The handler accepts supported camelCase/PascalCase aliases. If a host rejects a particular event, remove it manually and record reduced coverage. Hooks do not read transcripts, index repositories, run tests, deny operations, or force another turn on Stop.

Monitoring warns once for repeated unchanged retrieval, repeated failures, and overdue checkpoints where correlation is available. Native opaque events remain partial coverage. Check `doctor` and private `hook-health.json`; absent events do not establish monitoring coverage.

The Copilot command adapter reads failure diagnostics from the documented `error` field. Three observed failures with the same tool, arguments and error produce one advisory. A failure event does not prove that source is unchanged; its warning says so. A stable tool-call ID deduplicates repeated observations even when timestamps change. Without call IDs, timestamp-based correlation remains partial. Missing error text or an unobserved task-checkpoint binding is recorded in `hook-health.json`.

For `postToolUseFailure` only, guidance is plain stdout with exit code **2**, as required by the Copilot command-hook contract. Other successful handler invocations return JSON with exit code 0. Pre-tool and stop handlers never emit a denial or force continuation. Monitoring failures return `{}`/0. Verify the configured interpreter before enabling hooks: a missing Python executable or import failure happens before the handler and can cause the host to reject a pre-tool operation. No executable bit is needed for the JSON or Python module because the command invokes Python explicitly.

These output rules target the **Copilot SDK harness**, including its PascalCase aliases. Native **Local** hooks have separate output schemas; copying the same configuration does not establish equivalent behavior. To verify actual host delivery, select Copilot, use `/hooks` to inspect discovery, call `task_checkpoint`, then check the hook logs during a controlled missing-path/tool failure. Automated synthetic tests validate the command contract, not your installed Copilot version or its event availability.

The manual WebUI system prompt is a separate packaged resource, exported as `00_PROJECT_INSTRUCTIONS.md`. These Copilot agents are not the WebUI prompt.

## Compatibility references

The panel minimum is VS Code 1.85. Copilot agents, handoffs, native MCP and hooks have newer and independently evolving requirements. Validate them in your installed host.

Official references: [custom agents](https://code.visualstudio.com/docs/agent-customization/custom-agents), [MCP servers](https://code.visualstudio.com/docs/agent-customization/mcp-servers), [VS Code hooks](https://code.visualstudio.com/docs/agent-customization/hooks), [Copilot hooks](https://docs.github.com/en/copilot/reference/hooks-reference).

## Standalone MCP bootstrap

`python -m repopilotv3 --repo /absolute/repository mcp serve` starts before private initialization. Call `repository_status`, then `setup_repository` if needed. To use an existing child project, call `setup_repository` with `repository_path` and keep the returned `repository_id` in subsequent retrieval/checkpoint calls. Paths outside the configured root, private/generated directories, and escaping symlinks are rejected. Missing identifiers default to the original server root, never the last selected project. Subproject bindings are server-process local; after restart, call setup again to bind the same stable identifier. No models, restores, builds or harness installation run through these setup/status tools.

If a portable `.mcp.json` belongs to a separate local-model client, choose **Keep portable config and enable VS Code MCP**. That explicit choice leaves the portable file unchanged and registers the selected project separately with VS Code. Native `.vscode/mcp.json` duplicates still require migration or keeping the existing server.
