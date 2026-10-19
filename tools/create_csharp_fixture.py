"""Create a synthetic C# repository for RepoPilotV3 without proprietary source.

All RepoPilotV3 implementation code is Python. The C# strings below are test data
representing repositories the tool analyzes. Run ``python examples/create_demo.py
/path/to/demo``. Existing files are never overwritten.
"""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

FILES = {
    "Demo.csproj": '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net10.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings></PropertyGroup></Project>\n',
    "README.md": "# Synthetic add-in core\n\nCalculator implements ICalculator. Build with dotnet build. No Inventor installation is needed.\n",
    "ICalculator.cs": "namespace Demo;\n\n/// <summary>Arithmetic contract.</summary>\npublic interface ICalculator\n{\n    int Add(int left, int right);\n}\n",
    "Calculator.cs": "namespace Demo;\n\n/// <summary>Pure arithmetic used by an add-in.</summary>\npublic class Calculator : ICalculator\n{\n    public int Add(int left, int right)\n    {\n        return left + right;\n    }\n}\n",
    "CalculatorTests.cs": "namespace Demo;\n\npublic static class CalculatorTests\n{\n    public static bool AdditionWorks() => new Calculator().Add(2, 3) == 5;\n}\n",
    "Unrelated.cs": 'namespace Demo;\n\ninternal class Unrelated\n{\n    public string Description => "Unrelated report formatter";\n}\n',
    ".gitignore": "bin/\nobj/\n.repopilotv3/\n",
}


def create(
    destination: Path, framework: str = "net10.0", with_tests: bool = False, inventor: bool = False
) -> Path:
    """Write synthetic fixture files and initialize a Git repository without commits."""
    destination.mkdir(parents=True, exist_ok=True)
    if any((destination / name).exists() for name in FILES):
        raise FileExistsError("Destination already contains demo files")
    files = dict(FILES)
    if framework == "net48":
        files["Demo.csproj"] = (
            '<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net48</TargetFramework>'
            "<LangVersion>latest</LangVersion></PropertyGroup><ItemGroup>"
            '<PackageReference Include="Microsoft.NETFramework.ReferenceAssemblies" Version="1.0.3" PrivateAssets="All" />'
            "</ItemGroup></Project>\n"
        )
    files["Demo.csproj"] = files["Demo.csproj"].replace(
        "</Project>", '<ItemGroup><Compile Remove="Tests/**;interop/**" /></ItemGroup></Project>'
    )
    if with_tests:
        files["Tests/Calculator.Tests.csproj"] = f"""<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup><TargetFramework>{framework}</TargetFramework><IsTestProject>true</IsTestProject><LangVersion>latest</LangVersion></PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="../Demo.csproj" />
    <PackageReference Include="Microsoft.NET.Test.Sdk" Version="17.14.1" />
    <PackageReference Include="MSTest.TestAdapter" Version="3.9.3" />
    <PackageReference Include="MSTest.TestFramework" Version="3.9.3" />
  </ItemGroup>
</Project>
"""
        files["Tests/AdditionTests.cs"] = """using Microsoft.VisualStudio.TestTools.UnitTesting;
namespace Demo.Tests;
[TestClass]
public class AdditionTests
{
    [TestMethod]
    public void AddsTwoValues() => Assert.AreEqual(5, new Calculator().Add(2, 3));
}
"""
    if inventor:
        files["interop/Interop.csproj"] = (
            """<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><TargetFramework>net48</TargetFramework><AssemblyName>Autodesk.Inventor.Interop</AssemblyName></PropertyGroup><ItemGroup><PackageReference Include="Microsoft.NETFramework.ReferenceAssemblies" Version="1.0.3" PrivateAssets="All" /></ItemGroup></Project>\n"""
        )
        files["interop/Contract.cs"] = """// Synthetic fixture, NOT the Autodesk API.
namespace Inventor { public interface ApplicationAddInServer { void Activate(object site); void Deactivate(); } }
"""
        files["AddIn.cs"] = """namespace Demo;
// Synthetic Inventor-shaped add-in used only to test direct assembly references.
public class AddIn : Inventor.ApplicationAddInServer
{
    public void Activate(object site) { }
    public void Deactivate() { }
}
"""
        files["Demo.csproj"] = files["Demo.csproj"].replace(
            "</Project>",
            '<ItemGroup><Reference Include="Autodesk.Inventor.Interop"><HintPath>interop/bin/Debug/net48/Autodesk.Inventor.Interop.dll</HintPath></Reference></ItemGroup></Project>',
        )
    for name, content in files.items():
        target = destination / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-b", "main", str(destination)], check=True, capture_output=True)
    return destination


def main() -> None:
    """Read the target directory and create the disposable example repository."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--framework", choices=["net10.0", "net48"], default="net10.0")
    parser.add_argument("--with-tests", action="store_true")
    parser.add_argument(
        "--inventor", action="store_true", help="Create synthetic net48 direct-interop fixture"
    )
    args = parser.parse_args()
    print(
        create(
            args.destination, "net48" if args.inventor else args.framework, args.with_tests, args.inventor
        ).resolve()
    )


if __name__ == "__main__":
    main()
