# Static Copilot workspace harness

`workspace/` is authoritative. The VSIX ships these exact files, unchanged. **Setup and the extension never create, copy, merge, or rewrite them in your workspace.** Package staging is separate from workspace installation.

## Manual placement

1. Open **RepoPilotV3: Open Harness Folder**, or open this directory in the source checkout.
2. Read your existing workspace instructions/MCP entries first.
3. Manually copy the four files from `workspace/.github/agents/` into your workspace's `.github/agents/`.
4. Manually merge `workspace/.github/copilot-instructions.md` into your existing instructions. Keep only the RepoPilotV3-specific language/docstring paragraph when working on RepoPilot itself; adapt project conventions for other repositories.
5. Choose **one** MCP format below and manually merge its `repopilotv3` entry. Preserve unrelated entries.
6. Optionally copy `workspace/.github/hooks/repopilotv3.json` if your installed Copilot runtime supports that format/events. Check runtime logs after the first session.
7. Run `python -m repopilotv3 --repo /absolute/repository setup` in the selected environment. Select an agent explicitly in Copilot.

No runtime installer, generated agent template, or automatic upgrade is supplied. Your manually placed files remain yours. Review future changes and merge them deliberately.

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
