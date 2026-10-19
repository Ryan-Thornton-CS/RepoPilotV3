"""Python-only Roslyn bridge using assemblies already shipped in a local .NET SDK.

Run as ``python -m repopilotv3.analysis.roslyn``. The worker accepts one JSON request per
stdin line and emits one JSON response. Python.NET hosts CoreCLR in this isolated
process; RepoPilot contains no C# implementation or separately compiled worker.
The SDK's dotnet-format assemblies are discovered, never downloaded implicitly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from repopilotv3.core import PilotError, dotnet_env


def sdk_location(executable: str, root: str) -> Path:
    """Resolve the SDK selected by global.json using an explicit dotnet executable."""
    env = dotnet_env(executable)
    selected = subprocess.run(
        [executable, "--version"], cwd=root, env=env, text=True, capture_output=True, timeout=30, check=True
    ).stdout.strip()
    listing = subprocess.run(
        [executable, "--list-sdks"], cwd=root, env=env, text=True, capture_output=True, timeout=30, check=True
    ).stdout
    for line in listing.splitlines():
        match = re.match(r"^(\S+) \[(.+)]$", line)
        if match and match[1] == selected:
            return Path(match[2]) / selected
    raise PilotError("The selected SDK is missing from dotnet --list-sdks", "missing_sdk")


def load_roslyn(executable: str, root: str) -> SimpleNamespace:
    """Load the selected SDK's CoreCLR and Roslyn workspace assemblies locally."""
    sdk = sdk_location(executable, root)
    tool = sdk / "DotnetTools/dotnet-format"
    runtime_config = tool / "dotnet-format.runtimeconfig.json"
    if not runtime_config.exists():
        raise PilotError(
            "SDK lacks dotnet-format Roslyn assemblies; install a full .NET 10 SDK", "missing_roslyn"
        )
    sdk_root = sdk.parent.parent
    os.environ.update(dotnet_env(executable))
    os.environ["DOTNET_ROOT"] = str(sdk_root)
    from pythonnet import load

    load("coreclr", runtime_config=str(runtime_config), dotnet_root=str(sdk_root))
    import clr

    sys.path[:0] = [str(tool), str(tool / "BuildHost-netcore"), str(sdk)]
    clr.AddReference(str(tool / "BuildHost-netcore/Microsoft.Build.Locator.dll"))
    from Microsoft.Build.Locator import MSBuildLocator

    if not MSBuildLocator.IsRegistered:
        MSBuildLocator.RegisterMSBuildPath(str(sdk))
    for name in [
        "Microsoft.CodeAnalysis",
        "Microsoft.CodeAnalysis.CSharp",
        "Microsoft.CodeAnalysis.Workspaces",
        "Microsoft.CodeAnalysis.CSharp.Workspaces",
        "Microsoft.CodeAnalysis.Workspaces.MSBuild",
    ]:
        clr.AddReference(str(tool / f"{name}.dll"))
    from Microsoft.CodeAnalysis import (
        DocumentId,
        IMethodSymbol,
        INamedTypeSymbol,
        MetadataReference,
        OutputKind,
        ProjectId,
        SymbolDisplayFormat,
        XmlDocumentationProvider,
    )
    from Microsoft.CodeAnalysis.CSharp import CSharpCompilationOptions
    from Microsoft.CodeAnalysis.MSBuild import MSBuildWorkspace
    from Microsoft.CodeAnalysis.Text import SourceText
    from System import String
    from System.Collections.Generic import Dictionary
    from System.Threading import CancellationToken

    return SimpleNamespace(
        sdk=sdk,
        sdk_root=sdk_root,
        token=getattr(CancellationToken, "None"),
        DocumentId=DocumentId,
        IMethodSymbol=IMethodSymbol,
        INamedTypeSymbol=INamedTypeSymbol,
        MetadataReference=MetadataReference,
        OutputKind=OutputKind,
        ProjectId=ProjectId,
        SymbolDisplayFormat=SymbolDisplayFormat,
        XmlDocumentationProvider=XmlDocumentationProvider,
        CSharpCompilationOptions=CSharpCompilationOptions,
        MSBuildWorkspace=MSBuildWorkspace,
        SourceText=SourceText,
        String=String,
        Dictionary=Dictionary,
    )


def symbol_id(symbol) -> str:
    """Return a stable assembly-qualified documentation ID for a Roslyn symbol."""
    assembly = str(symbol.ContainingAssembly.Name) if symbol.ContainingAssembly else "source"
    return assembly + ":" + str(symbol.GetDocumentationCommentId() or symbol.ToDisplayString())


def analyze(request: dict) -> dict:
    """Analyze captured C# documents, preserving source hashes and incomplete-analysis diagnostics."""
    root = Path(request["root"]).resolve()
    r = load_roslyn(request["sdk_executable"], str(root))
    files = {f["path"]: f for f in request["files"]}
    hashes = {p: hashlib.sha256(f["text"].encode()).hexdigest() for p, f in files.items()}
    if any(hashes[p] != files[p]["text_hash"] for p in files):
        raise PilotError("Worker input text checksum mismatch", "worker_snapshot_mismatch")
    diagnostics, projects, symbols, edges = [], [], [], set()
    properties = r.Dictionary[r.String, r.String]()
    properties["Configuration"] = request["configuration"].get("configuration", "Debug")
    properties["Platform"] = request["configuration"].get("platform", "AnyCPU")
    if request["configuration"].get("target_framework"):
        properties["TargetFramework"] = request["configuration"]["target_framework"]
    properties["RestoreDuringBuild"] = "false"
    workspace = r.MSBuildWorkspace.Create(properties)

    def workspace_failed(sender, event):
        """Capture workspace-load diagnostics without writing protocol noise to stdout."""
        diagnostics.append(str(event.Diagnostic.Message))

    workspace.WorkspaceFailed += workspace_failed
    try:
        for path in request.get("projects", []):
            full = str(root / path)
            if any(str(p.FilePath) == full for p in workspace.CurrentSolution.Projects):
                continue
            try:
                workspace.OpenProjectAsync(full).GetAwaiter().GetResult()
            except Exception as exc:
                diagnostics.append(f"{path}: {exc}")
        solution = workspace.CurrentSolution
        covered = set()
        for project in list(solution.Projects):
            for document in list(project.Documents):
                doc_path = Path(str(document.FilePath)).resolve() if document.FilePath else None
                relative = (
                    (
                        doc_path.relative_to(root).as_posix()
                        if doc_path.is_relative_to(root)
                        else str(doc_path)
                    )
                    if doc_path
                    else ""
                )
                if relative in files:
                    text = r.SourceText.From(files[relative]["text"])
                    solution = solution.WithDocumentText(document.Id, text)
                    covered.add(relative)
                else:
                    solution = solution.RemoveDocument(document.Id)
        loose = sorted(p for p in set(files) - covered if not files[p].get("auxiliary"))
        if loose:
            project_id = r.ProjectId.CreateNewId()
            solution = solution.AddProject(project_id, "Unresolved files", "RepoPilot.Unresolved", "C#")
            solution = solution.WithProjectCompilationOptions(
                project_id, r.CSharpCompilationOptions(r.OutputKind.DynamicallyLinkedLibrary)
            )
            ref_dirs = list((r.sdk_root / "packs/Microsoft.NETCore.App.Ref").glob("*/ref/net10.0"))
            refs = list(sorted(ref_dirs)[-1].glob("*.dll")) if ref_dirs else []
            for dll in refs:
                solution = solution.AddMetadataReference(
                    project_id, r.MetadataReference.CreateFromFile(str(dll))
                )
            for path in loose:
                solution = solution.AddDocument(
                    r.DocumentId.CreateNewId(project_id),
                    Path(path).name,
                    r.SourceText.From(files[path]["text"]),
                    filePath=str(root / path),
                )
            diagnostics.append(
                "Loose/unresolved files use .NET 10 fallback references; target-specific analysis is partial."
            )
        for project in list(solution.Projects):
            for dll in request["interop_paths"] + request["reference_assembly_paths"]:
                path = Path(dll)
                if not path.is_file() or path.suffix.lower() != ".dll":
                    diagnostics.append(f"Missing/non-DLL metadata reference: {dll}")
                    continue
                try:
                    xml = path.with_suffix(".xml")
                    if xml.is_file():
                        reference = r.MetadataReference.CreateFromFile(
                            str(path), documentation=r.XmlDocumentationProvider.CreateFromFile(str(xml))
                        )
                    else:
                        reference = r.MetadataReference.CreateFromFile(str(path))
                    solution = solution.AddMetadataReference(project.Id, reference)
                except Exception as exc:
                    diagnostics.append(f"Reference {dll}: {exc}")
        for project in solution.Projects:
            projects.append(
                {
                    "name": str(project.Name),
                    "path": Path(str(project.FilePath)).relative_to(root).as_posix()
                    if project.FilePath and Path(str(project.FilePath)).is_relative_to(root)
                    else None,
                    "assembly": str(project.AssemblyName),
                    "dependencies": [
                        str(solution.GetProject(x.ProjectId).Name) for x in project.ProjectReferences
                    ],
                }
            )
            compilation = project.GetCompilationAsync(r.token).GetAwaiter().GetResult()
            if compilation is None:
                continue
            diagnostics.extend(str(d) for d in compilation.GetDiagnostics() if str(d.Severity) == "Error")
            for document in project.Documents:
                if not document.FilePath:
                    continue
                doc_path = Path(str(document.FilePath)).resolve()
                path = (
                    doc_path.relative_to(root).as_posix() if doc_path.is_relative_to(root) else str(doc_path)
                )
                if path not in files:
                    continue
                text = str(document.GetTextAsync(r.token).GetAwaiter().GetResult().ToString())
                if hashlib.sha256(text.encode()).hexdigest() != hashes[path]:
                    raise PilotError(
                        "Roslyn document differs from captured source", "worker_snapshot_mismatch"
                    )
                if files[path].get("auxiliary"):
                    continue
                tree = document.GetSyntaxTreeAsync(r.token).GetAwaiter().GetResult()
                model = compilation.GetSemanticModel(tree)
                nodes = list(tree.GetRoot().DescendantNodes())
                declaration_kinds = {
                    "ClassDeclaration",
                    "StructDeclaration",
                    "InterfaceDeclaration",
                    "RecordDeclaration",
                    "RecordStructDeclaration",
                    "EnumDeclaration",
                    "MethodDeclaration",
                    "ConstructorDeclaration",
                    "PropertyDeclaration",
                    "EventDeclaration",
                    "DelegateDeclaration",
                    "VariableDeclarator",
                }
                for node in nodes:
                    kind = str(node.Kind())
                    if kind in declaration_kinds:
                        if kind == "VariableDeclarator" and str(node.Parent.Parent.Kind()) not in {
                            "FieldDeclaration",
                            "EventFieldDeclaration",
                        }:
                            continue
                        symbol = model.GetDeclaredSymbol(node, r.token)
                        if symbol is None:
                            continue
                        span = node.GetLocation().GetLineSpan()
                        symbols.append(
                            {
                                "id": symbol_id(symbol),
                                "name": str(symbol.Name),
                                "kind": str(symbol.Kind),
                                "signature": str(
                                    symbol.ToDisplayString(r.SymbolDisplayFormat.MinimallyQualifiedFormat)
                                ),
                                "accessibility": str(symbol.DeclaredAccessibility),
                                "namespace": str(symbol.ContainingNamespace),
                                "path": path,
                                "start": span.StartLinePosition.Line + 1,
                                "end": span.EndLinePosition.Line + 1,
                                "summary": str(symbol.GetDocumentationCommentXml(None, False, r.token) or ""),
                                "project": str(project.Name),
                                "is_test": any(
                                    str(a.AttributeClass.Name)
                                    in {
                                        "TestMethodAttribute",
                                        "TestClassAttribute",
                                        "FactAttribute",
                                        "TheoryAttribute",
                                        "TestAttribute",
                                    }
                                    for a in symbol.GetAttributes()
                                ),
                            }
                        )
                        if str(symbol.Kind) == "NamedType":
                            type_symbol = r.INamedTypeSymbol(symbol)
                            if type_symbol.BaseType:
                                edges.add((symbol_id(symbol), symbol_id(type_symbol.BaseType), "inherits"))
                            for iface in type_symbol.AllInterfaces:
                                edges.add((symbol_id(symbol), symbol_id(iface), "implements"))
                                for member in iface.GetMembers():
                                    impl = type_symbol.FindImplementationForInterfaceMember(member)
                                    if impl:
                                        edges.add((symbol_id(impl), symbol_id(member), "implements_member"))
                        if str(symbol.Kind) == "Method":
                            method = r.IMethodSymbol(symbol)
                            if method.OverriddenMethod:
                                edges.add(
                                    (symbol_id(method), symbol_id(method.OverriddenMethod), "overrides")
                                )
                    if kind in {
                        "InvocationExpression",
                        "ObjectCreationExpression",
                        "IdentifierName",
                        "AddAssignmentExpression",
                    }:
                        target = model.GetSymbolInfo(node, r.token).Symbol
                        source = model.GetEnclosingSymbol(node.SpanStart, r.token)
                        if target and source:
                            relation = "calls" if kind == "InvocationExpression" else "references"
                            edges.add((symbol_id(source), symbol_id(target), relation))
                        if kind == "AddAssignmentExpression":
                            target = model.GetSymbolInfo(node.Left, r.token).Symbol
                            if target and source and str(target.Kind) == "Event":
                                edges.add((symbol_id(source), symbol_id(target), "event_subscription"))
        return {
            "quality": "partial_semantic" if diagnostics else "semantic",
            "symbols": symbols,
            "projects": projects,
            "diagnostics": list(dict.fromkeys(diagnostics))[:200],
            "relationships": [{"source": s, "target": t, "kind": k} for s, t, k in sorted(edges)],
            "document_hashes": {p: f["hash"] for p, f in files.items() if not f.get("auxiliary")},
            "text_hashes": hashes,
        }
    finally:
        workspace.Dispose()


def main() -> int:
    """Serve one analysis per isolated process to avoid mixing SDK/runtime versions."""
    line = sys.stdin.readline(20_000_001)
    try:
        if len(line) > 20_000_000:
            raise PilotError("Analysis request exceeds 20 MB")
        request = json.loads(line)
        if request.get("version") != "1.0" or request.get("operation") != "analyze":
            raise PilotError("Unsupported worker protocol")
        response = analyze(request)
    except Exception as exc:
        response = {"error": str(exc)}
    print(json.dumps(response, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
