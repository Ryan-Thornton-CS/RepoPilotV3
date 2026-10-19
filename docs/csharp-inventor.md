# C#, SDKs and Inventor 2024

RepoPilot's implementation is Python. It analyzes your C# projects without adding a C# implementation worker or redistributing Autodesk binaries. Native Windows is recommended for Inventor projects; Visual Studio is not a prerequisite.

## SDK and optional semantics

Install a user-owned .NET 10 SDK explicitly, use the already bundled Python.NET bindings (source developers select the `csharp` extra). Configure `.repopilotv3/config.json` locally:

```json
{
  "dotnet": "C:/Users/ryan/.dotnet/dotnet.exe",
  "interop_paths": ["C:/Program Files/Autodesk/Inventor 2024/Bin/Public Assemblies/Autodesk.Inventor.Interop.dll"],
  "reference_assembly_paths": ["C:/Program Files (x86)/Reference Assemblies/Microsoft/Framework/.NETFramework/v4.8"],
  "analysis": {"configuration":"Debug","platform":"AnyCPU","target_framework":"net48"}
}
```

Merge these fields into the existing initialized config; do not replace ownership/root fields. Use the actual DLL path on your machine. Accompanying local XML documentation is read when present. No Autodesk binary is packaged or copied into source exports.

The backend detects the configured SDK and hosts Roslyn through Python.NET in an isolated Python worker process. Analysis resolves project references, configured assembly/reference paths, inherited/interface relationships, references/callers and tests where possible. Missing SDK/Python.NET/references or project-load issues produce explicit diagnostics and text-only fallback. Dynamic runtime connections remain outside static guarantees.

`REPOPILOTV3_DOTNET` can select the executable at initialization. SDK discovery checks PATH and known user-space installations. After configuration, `doctor` reports the selected SDK, framework reference paths, interop files, runner settings, project-load issues and degraded analysis. No dependency restore occurs as part of indexing.

## Preserve project configuration

Keep existing `.csproj` files, target frameworks, direct DLL `<Reference>` entries and `HintPath` values. RepoPilot does not migrate projects or replace your build tools. `ResolveComReference` is unsupported in the MSBuild used by `dotnet`; use existing direct interop references or the project's explicitly selected compatible build environment rather than silently rewriting it. See [Microsoft's task documentation](https://learn.microsoft.com/en-us/visualstudio/msbuild/resolvecomreference-task).

For .NET Framework 4.8, reference assemblies must be available through installed targeting packs or an explicitly provisioned reference-assembly package. Building a synthetic SDK fixture is different from loading a real Inventor add-in.

## Validation profiles

Profiles are developer-authored configuration, never model command strings. Example:

```json
{
  "profiles": {
    "build": {"kind":"build","args":["Addin.csproj"],"timeout":300},
    "tests": {"kind":"test","args":["Tests/Tests.csproj"],"runner":"VSTest","timeout":300}
  },
  "default_validation": "tests"
}
```

Managed .NET invocations append `--no-restore`. Explicitly provision packages beforehand with your chosen tool/feed. SDK child environments disable telemetry, first-time setup and background workload update notification. Arbitrary MSBuild targets may have their own side effects/network access.

.NET 10 supports VSTest and Microsoft Testing Platform with differing options. Select the runner explicitly, or allow `global.json` detection. MTP projects must already include their TRX reporting capability; RepoPilot does not install it. A zero exit code with no executed-test evidence is inconclusive. Official reference: [dotnet test](https://learn.microsoft.com/en-us/dotnet/core/tools/dotnet-test).

## WSL driving Windows analysis

Keep the main backend/database owned by the Linux host in a WSL window. If Windows project loading is required, explicitly configure a Windows Python worker that already has RepoPilotV3 and Python.NET installed:

```json
{
  "worker_command": ["/mnt/c/Users/ryan/venvs/pilot/Scripts/python.exe", "-m", "repopilotv3.analysis.roslyn"],
  "dotnet": "/mnt/c/Users/ryan/.dotnet/dotnet.exe",
  "path_mappings": [{"local":"/mnt/c/work/Addin","worker":"C:/work/Addin"}]
}
```

Use paths meaningful to the selected host/worker. Source/document identities travel as repository-relative paths; the configured mapping supplies the worker root. A separate native Windows backend needs its own private state location. Never share a live SQLite database between Windows and WSL processes.

## Explicit synthetic and application checks

`tools/create_csharp_fixture.py` generates disposable C# test data: .NET 10, Framework 4.8 and an Inventor-shaped direct-reference fixture with a synthetic interop stub. `tools/provision_fixtures.py` explicitly restores pinned test/reference dependencies. These are generated test inputs, not implementation code or Autodesk binaries.

Before claiming real Inventor compatibility, manually build and load the actual add-in in Inventor 2024 on native Windows, verify registration/runtime/bitness, exercise the changed feature and inspect application diagnostics. Synthetic tests cannot establish that result.
