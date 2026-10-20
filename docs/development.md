# Development and verification

## Source layout

`src/repopilotv3/` contains documented Python services; `extension/src` and `extension/media` contain the VS Code UI. `harness/` is authoritative static configuration. `tests/`, `extension/test`, `examples/` and `tools/` hold synthetic verification and build utilities. Release artifacts are a complete VSIX and a source-plus-harness distribution ZIP in ignored `dist/`; generated dependencies never belong in Git.

## Explicit build provisioning

Use Python 3.12+, **uv 0.12.13** and **Node 22**. From this repository:

```bash
uv sync --frozen --group dev --extra csharp
npm ci --prefix extension --ignore-scripts
uv run --frozen --group dev python tools/build.py
```

This builds the pure-Python backend internally, stages CPython and hash-pinned dependencies for Windows x64 and Linux x64, compiles the extension, and writes `dist/repopilotv3-0.2.14.vsix`. `npm run package --prefix extension` invokes the same build. There is no publication or workspace harness installation.

`uv.lock` and `extension/package-lock.json` pin Python/Node dependencies. `tools/runtime-lock.json` pins Python build URLs, hashes and the uv metadata release. uv verifies standalone runtime downloads against its pinned metadata. pip installs only hash-verified binary wheels; target-specific markers are evaluated explicitly before cross-platform installation. Build caches live under `.tools/bundle/`; staging lives under ignored `extension/backend`, `extension/docs`, and `extension/harness`.

After a successful online provisioning/build, `tools/build.py --offline` rebuilds from the local uv/build cache and wheelhouses. Development tools and caches must already exist. Installed extensions never create environments, invoke pip/uv, or download Python. Runtime licenses and dependency distribution metadata are retained in each bundled Python tree.

The universal VSIX is larger because it carries two native runtimes. No system Python is required; SDKs/Autodesk binaries remain external. To add architectures, pin a standalone runtime and compatible wheels, update the selector and run native smoke tests before advertising support.

## Checks

```bash
uv run --frozen ruff check src tests tools examples
uv run --frozen pytest -q
npm test --prefix extension
uv run --frozen python tools/verify.py --smoke
```

The package verifier compares static harness bytes, checks both runtime/platform manifests and native binaries, and exercises the extracted host runtime outside the source checkout. Its synthetic workflow covers initialization, TODO, read, full-file proposal conversion, reviewed patch application, a genuine check plus JUnit-evidenced pytest execution, and fresh-chat handoff. Dependency imports are checked with outbound socket connections denied. This verifies managed operations; it cannot constrain arbitrary approved build targets.

Run real editor-host tests **after building** so they use bundled Python, not the developer environment:

```bash
VSCODE_TEST_VERSION=1.85.2 npm run test:editor --prefix extension
VSCODE_TEST_VERSION=stable npm run test:editor --prefix extension
```

Linux CI uses `xvfb-run -a`; Windows uses the same script without Xvfb. Legacy VS Code 1.85.2 runs on Ubuntu 22.04, while current stable runs on ubuntu-latest. Both editors also run on Windows. The extension deactivation/test teardown awaits backend exit and closes test editors. CI retains editor logs on failure; crashes remain failures. These explicit tests may download editor test builds. The runner removes inherited Electron/VS Code flags and launches the editor directly with an argument array, preserving spaces, Unicode and UNC paths without a command shell. Successful checks require both recorded assertion/teardown evidence and a clean editor exit. The test opens native read-only diffs, checks dirty editor guards, clipboard access, extension discovery and real backend round trips. CI also runs Python tests on both operating systems. A Linux/WSL pass does not establish native Windows GUI or real Inventor compatibility.

Optional SDK fixture checks use `tools/provision_fixtures.py --dotnet <executable>` for deliberate restore, followed by `REPOPILOTV3_TEST_DOTNET=<executable> pytest -q`. Native .NET Framework fixtures run only on Windows. See [C#/Inventor](csharp-inventor.md); real application loading remains a documented manual check.

`tools/benchmark.py --output .tools/reports/token-control.json` measures deterministic local call counts, returned context, repetitions and cache/hook timing. These are not measured model billing or guaranteed token savings. Reports/build artifacts are generated locally instead of checked-in historical copies.

## Manual release smoke check

Install the built VSIX in a fresh profile with no configured Python path. Trust a synthetic workspace, open RepoPilot, create a session, run TODO/read, inspect a proposal in native diffs, approve and verify an approved test profile. Restart VS Code and confirm history/draft restoration without replay. Generate a handoff and confirm distinct chat IDs. Copy the static harness manually to separately check Copilot discovery/MCP/hooks on supported hosts.

The VSIX bundles the pinned Microsoft jsonc-parser runtime and license under out/vendor for JSONC-preserving editor configuration edits. Integration tests cover native-provider registration through a public-API adapter, explicit duplicate migration, standalone real MCP bootstrap, harness conflicts and rollback. Editor-host checks exercise the actual API registration where available and generated bundled assets. For manual Copilot verification, enable a project, inspect it in MCP: List Servers, approve/start it and call repository_status from your selected model; a provider registration alone is not proof of actual model-tool execution.

## Distribution ZIP and UI documentation

After verifying the full VSIX, run `uv run --frozen python tools/release.py`. It writes the versioned `*-release.zip` and outer `dist/SHA256SUMS`. The ZIP contains the VSIX, loose `.github` workspace harness, both MCP configuration formats, complete backend sources/metadata/locks, license and setup notes. Internal checksums cover every payload member. Inputs are explicitly selected; private state and caches are excluded. CI uploads both artifacts on its existing triggers without publishing releases or Marketplace extensions. Version selection comes from project metadata instead of a hardcoded extraction filename.

`docs/help.json` supplies data-only offline help. Its PNG figures are real panel captures with synthetic content and numbered red outlines. To refresh them on Linux, explicitly provision a cached test editor, compile the extension and stage its bundled backend/docs, then run `uv run --frozen python tools/capture_ui.py --launch`. The tool connects only to an explicitly launched local Chromium debugging endpoint; it does not automate external WebUIs. `extension/test/capture.ts` seeds disposable source and task records under ignored `.tools/`. No screenshots of private projects are needed.


Windows CI compares native filesystem identities, including 8.3 and junction aliases, across the Python handshake, MCP duplicate detection and unsaved-buffer checks. Regression tests also exercise LF and CRLF rollback independently and decode shipped Markdown explicitly as UTF-8. Matrix fail-fast is disabled so a Windows failure does not cancel independent Linux/editor checks. The package job uploads the full VSIX and distribution ZIP; a failed verification matrix can coexist with those downloadable artifacts and must still be fixed before treating the run as passing.
