"""Capture annotated synthetic panel screenshots from an explicitly launched test editor.

Launch extension/test/capture.ts in a disposable editor with --remote-debugging-port=9333,
then run this tool. It connects only to the local Chromium debugging endpoint.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import subprocess
import tempfile
import base64
import json
import urllib.request
from pathlib import Path

from websockets.asyncio.client import connect


class PanelCapture:
    """Use Chromium's local screenshot API; repository text remains synthetic display data."""

    def __init__(self):
        """Discover the explicitly launched local editor and RepoPilot webview targets."""
        with urllib.request.urlopen("http://127.0.0.1:9333/json", timeout=5) as response:
            targets = json.load(response)
        self.page = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "page")
        self.frame = next(t["webSocketDebuggerUrl"] for t in targets if t["type"] == "iframe" and "repopilot-local.repopilotv3" in t["url"])

    async def call(self, address: str, method: str, params: dict):
        """Perform one localhost-only debugging request and report errors explicitly."""
        async with connect(address, max_size=20000000) as socket:
            await socket.send(json.dumps({"id": 1, "method": method, "params": params}))
            while True:
                result = json.loads(await socket.recv())
                if result.get("id") == 1:
                    if "error" in result:
                        raise RuntimeError(result["error"])
                    return result["result"]

    async def annotate(self, selectors: list[str], drawer: bool) -> None:
        """Add numbered red outlines without modifying source files or execution behavior."""
        script = """(() => {
          const d=document.querySelector('iframe').contentDocument;
          for (const old of d.querySelectorAll('.documentation-callout')) old.remove();
          d.getElementById('session-drawer').hidden=DRAWER;
          const transcript=d.getElementById('transcript'); transcript.scrollTop=transcript.scrollHeight;
          SELECTORS.forEach((selector,index)=>{
            const target=d.querySelector(selector), r=target.getBoundingClientRect();
            const outline=d.createElement('div'); outline.className='documentation-callout';
            Object.assign(outline.style,{position:'fixed',left:r.left+'px',top:r.top+'px',width:r.width+'px',height:r.height+'px',border:'2px solid #ff4545',boxSizing:'border-box',pointerEvents:'none',zIndex:'10000'});
            const number=d.createElement('span'); number.textContent=String(index+1);
            Object.assign(number.style,{position:'absolute',right:'0',top:'0',background:'#b80000',color:'#fff',font:'bold 12px sans-serif',padding:'1px 4px'});
            outline.append(number);d.body.append(outline);
          }); return true;
        })()""".replace("SELECTORS", json.dumps(selectors)).replace("DRAWER", "false" if drawer else "true")
        await self.call(self.frame, "Runtime.evaluate", {"expression": script, "returnByValue": True})

    async def save(self, target: Path) -> None:
        """Capture the actual webview rectangle from the editor window as a PNG."""
        result = await self.call(self.page, "Runtime.evaluate", {"expression": "JSON.stringify([...document.querySelectorAll('iframe')].find(f=>f.src.includes('repopilot-local.repopilotv3')).getBoundingClientRect())", "returnByValue": True})
        rect = json.loads(result["result"]["value"])
        screenshot = await self.call(self.page, "Page.captureScreenshot", {"format": "png", "clip": {"x": rect["x"], "y": rect["y"], "width": rect["width"], "height": rect["height"], "scale": 1}})
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(base64.b64decode(screenshot["data"]))

    async def capture(self, root: Path) -> None:
        """Produce the two versioned help figures from the same synthetic panel."""
        await self.annotate(["#top-tabs", ".project-bar", "#project-tabs", "header", "#session-drawer"], True)
        await self.save(root / "docs/images/projects-guide.png")
        await self.annotate(["#request", "#send", "#paste", "#todo-toggle", "#update-context", "#options-toggle"], False)
        await self.save(root / "docs/images/chat-guide.png")


async def launch_capture(root: Path) -> None:
    """Launch an isolated cached editor and stop it after explicit synthetic capture."""
    area = root / ".tools/ui-capture"
    area.mkdir(parents=True, exist_ok=True)
    workspace = area / "Flight Demo"
    workspace.mkdir(exist_ok=True)
    for name in ["ready", "done"]:
        (area / name).unlink(missing_ok=True)
    editors = sorted((root / "extension/.vscode-test").glob("vscode-linux-x64-*/code"))
    editors.sort(key=lambda editor: "1.85.2" not in str(editor))
    if not editors:
        raise RuntimeError("Provision a test editor explicitly before capturing documentation")
    env = {key: value for key, value in os.environ.items() if not key.startswith("VSCODE_") and key != "ELECTRON_RUN_AS_NODE"}
    env.update(REPOPILOTV3_CAPTURE_READY=str(area / "ready"), REPOPILOTV3_CAPTURE_DONE=str(area / "done"))
    with tempfile.TemporaryDirectory(prefix="RepoPilot-docs-") as user_data, (area / "editor.log").open("w") as log:
        process = subprocess.Popen([str(editors[0]), str(workspace), "--disable-workspace-trust",
            "--disable-extensions", "--no-sandbox", "--disable-gpu", "--remote-debugging-port=9333",
            "--user-data-dir=" + user_data, "--extensionDevelopmentPath=" + str(root / "extension"),
            "--extensionTestsPath=" + str(root / "extension/out/test/capture.js")], env=env, stdout=log, stderr=subprocess.STDOUT)
        try:
            for attempt in range(100):
                if (area / "ready").exists():
                    try:
                        capture = PanelCapture()
                        result = await capture.call(capture.frame, "Runtime.evaluate", {
                            "expression": "document.querySelector('iframe')?.contentDocument?.getElementById('title')?.textContent", "returnByValue": True})
                        if result["result"].get("value") == "Add greeting test":
                            await capture.capture(root)
                            return
                    except (OSError, StopIteration, KeyError, RuntimeError):
                        pass
                await asyncio.sleep(0.5)
            raise RuntimeError("Synthetic panel did not become ready; inspect .tools/ui-capture/editor.log")
        finally:
            (area / "done").write_text("done")
            try:
                await asyncio.to_thread(process.wait, timeout=15)
            except subprocess.TimeoutExpired:
                process.terminate()
                await asyncio.to_thread(process.wait, timeout=5)


def main() -> None:
    """Capture an already running fixture, or explicitly launch a provisioned test editor."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--launch", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).parents[1]
    asyncio.run(launch_capture(root) if args.launch else PanelCapture().capture(root))


if __name__ == "__main__":
    main()
