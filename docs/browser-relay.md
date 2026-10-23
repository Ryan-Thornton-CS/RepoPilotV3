# Experimental browser relay

The ordinary copy/paste workflow remains available for every provider. The relay is **experimental, off by default**, and limited to compatible native VS Code browser tools. It does not use another model, install a browser, or test your private provider for compatibility.

## Open a chat

Under **Chat**:

- **Set WebUI address** saves an HTTP/HTTPS project default or session override. Saving does not check DNS, make an HTTP request, or navigate to it. Private hosts and ports are accepted.
- **Open WebUI** opens that address in the integrated browser where available, otherwise your external browser.
- **Copy latest reply + Open WebUI** copies the latest completed harness response and opens the address. You still paste/send it yourself unless you separately configure the relay.

**Open controllable chat tab** in the experimental options explicitly opens a native-tool-owned integrated tab. Use it if an ordinary browser tab is not accessible to the tools. Sign in there; its browser session can be isolated from your other tabs.

Login happens normally inside the browser. RepoPilot does not collect passwords, cookies or authentication exports.

## Configure the picker

1. Open a local RepoPilot session and your browser chat. Have an example user message and model reply visible.
2. Choose **Chat → Experimental browser relay → Enable**.
3. Choose **Select chat controls** and select the integrated-browser tab. Normal VS Code browser permissions still apply.
4. Pick the message input. Hover highlights an element; click selects without activating it. Escape cancels. Preview and confirm the CSS selector.
5. For sending/capture, also select Send, a user-message bubble, and a model-message bubble. Bubble selectors must match repeated messages of that role, not the whole transcript or a single positional bubble.
6. Configure either a generating control that appears during generation, or a selector matching the newest model bubble only when complete. For sites with ambiguous completion, use fill-only/manual operation.
7. Choose a mode and click **Start**. Settings alone never start automation.

The first picker supports the main document. Chats inside iframes, closed shadow roots or canvas-only controls require manual operation. CSS selectors can be entered/refined, but arbitrary scripts cannot be configured. Page changes may require selecting controls again.

## Modes

| Mode | Automatic behavior | Your action |
|---|---|---|
| Fill the message only | Fill each completed harness response | Click Send; import the model reply |
| Send and capture | Fill, send and capture a completed reply | Review it, then choose Import + Run |
| Full tool loop | Also import model requests through the normal execution pipeline | Review patches, grant commands and answer questions |

Only one mode is selected. Stop/Pause, local errors, approvals and uncertain delivery interrupt the loop. Ten model turns per Start is the default budget; adjust it in the experimental options. A model reply without tool requests pauses for your input.

Starting again never blindly retries a transfer already journaled as filled/sending/sent. Inspect the browser first; continue with the normal manual controls when delivery is uncertain. Automatic browser transfers never overwrite your clipboard or confirm context-file uploads. Session/project changes, reconnects and editor restarts disarm the relay.

## Local demo

Use `examples/create_demo.py` to create a synthetic source folder and add it as a RepoPilot project if you want an isolated test.

1. Choose **Chat → Experimental browser relay → Open local demo chat**. Python serves three bundled assets on `127.0.0.1` using an available ephemeral port. In remote/WSL workspaces, VS Code resolves/forwards the localhost address; it may ask for permission.
2. The page displays **Scripted demo — no model connected**. Its example bubbles allow picking before you start a conversation.
3. Enable the experiment and run **Select chat controls**. Pick the textarea, Send, the example user bubble and the example model bubble. Stable attributes generate these mappings:

   ```text
   Input: textarea[data-testid="chat-input"]
   Send: button[data-testid="send-message"]
   Sent: article[data-role="user"]
   Reply: article[data-role="assistant"]
   Complete: article[data-role="assistant"][data-complete="true"]
   ```

4. The picker recognizes the demo bubble’s `data-complete` attribute. Skip the busy control and keep the suggested completion selector.
5. Create a local session, copy/prepare its starter, configure its controls, choose **Fill the message only**, then Start. Inspect the input and click Send yourself. Paste the streamed model reply into RepoPilot as usual.
6. For a fresh test, stop/reset the demo and create a new local session. Select its controls again, choose **Send and capture** or **Full tool loop**, then Start. The canned sequence initializes task/TODO, searches for README, then stops with a completion message. It requests no patches or commands.
7. Click **Stop generating** during a reply to test interruption. The partial bubble remains incomplete. **Reset demo** clears page memory and creates new request IDs; it does not erase RepoPilot task/history records.
8. Choose **Stop local demo** when finished. Backend shutdown/reconnect also stops it. A normal editor restart does not recreate it.

There are no provider calls, external assets or stored browser transcripts. Viewing the demo in an external browser is possible when native browser support is missing, but this does not test the relay.

## Availability and limitations

Opening the integrated browser and controlling it are separate capabilities. Relay diagnostics feature-detect native page-listing and Playwright tools; your editor version, browser settings and organization permissions determine availability. No proposed API is required by the extension. When unavailable, the panel and manual workflow still work.

The transfer code follows fixed rules, but that does not guarantee successful delivery. Login interruptions, UI changes and lost acknowledgements remain possible. RepoPilot pauses rather than automatically duplicating a message. The localhost demo verifies orchestration; it does not establish compatibility with a provider or prove model correctness.
