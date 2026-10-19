/* Chat presentation only. All repository/model text is data, never executable markup. */
'use strict';
const vscode = acquireVsCodeApi();
const byId = id => document.getElementById(id);
const saved = vscode.getState() || { drafts: {}, session: null };
let session = saved.session;
let entries = new Map();
let firstRender = true;
let draftTimer;
let searchTimer;
const streams = new Map();
const send = (action, extra = {}) => vscode.postMessage({ action, ...extra });
const descriptions = Object.freeze({
  projectPrompt: 'Copy reusable project behavior and tool instructions (up to 8,000 characters). Paste into WebUI project settings.',
  starter: 'Copy this chat’s introduction, mode and task state. Project instructions are separate.',
  handoff: 'Prepare progress and context for a fresh WebUI chat.',
  rename: 'Change the chat’s title.',
  archive: 'Hide a chat or bring it back from the archived list.',
  delete: 'Remove chat history and drafts; retain shared tasks and evidence.',
  setup: 'Choose the approved build/test check used after patches.',
  limit: 'Set when to suggest starting a fresh chat.',
  permission: 'Allow or revoke model-requested commands for this session.',
  project: 'Identify the WebUI project sharing uploaded context.',
  todo: 'Manually save progress when the model cannot provide it.',
  workspace: 'Choose an individual project folder, including a subfolder in a multi-project workspace.',
  harness: 'Open the static Copilot agents, MCP configuration and hooks.',
  diagnostics: 'Inspect backend, SDK and environment information.',
  restart: 'Restart the local connection without replaying requests.',
  settings: 'Open RepoPilot’s VS Code settings.',
  new: 'Create a chat and prepare its starter.',
  select: 'Open this saved chat without replaying requests.',
  send: 'Prepare typed text to paste into the WebUI.',
  pasteRun: 'Import a model reply and execute its tool requests.',
  run: 'Process the composer’s contents as a model response.',
  note: 'Record a note without executing tools.',
  stop: 'Cancel a running command.',
  copy: 'Copy this entire turn, including its tool results, errors and continuation guidance.',
  history: 'Show older transcript entries.',
  prepare: 'Refresh exports and show the upload checklist.',
  package: 'Open the generated upload package.',
  instructions: 'Copy the WebUI project prompt.',
  confirm: 'Record files you actually pasted or uploaded.',
  review: 'Inspect every file in the proposed changes using native diff tabs.',
  commandDiff: 'Inspect source changes observed after a command.',
  approve: 'Apply every proposed file change and run configured validation.',
  reject: 'Decline the proposed changes.',
  recover: 'Repair an interrupted patch application.',
  source: 'Open this cited source file at its referenced line.'
});
const modes = Object.freeze({
  plan: 'Clarify requirements and organize work.',
  ask: 'Answer focused repository questions.',
  code: 'Write changes and validate them.',
  review: 'Examine changes and identify defects.'
});

/** Provide native hover help and an accessible description without interpreting markup. */
function describe(element, text) {
  element.title = text;
  element.setAttribute('aria-description', text);
}

/** Show the repository boundary in the options menu and its folder-picker tooltip. */
function projectRoot(workspace) {
  byId('workspace-root').textContent = workspace || 'No project selected';
  describe(document.querySelector('[data-action="workspace"]'), descriptions.workspace + (workspace ? ` Current project: ${workspace}` : ''));
}

/** Persist the selected draft without executing it on disposal or restart. */
function saveDraft() {
  if (!session) return;
  saved.drafts[session] = byId('request').value;
  saved.session = session;
  vscode.setState(saved);
  send('draft', { sessionId: session, text: saved.drafts[session] });
}

/** Grow wrapped drafts upward to 30% of the view height, then scroll inside the input. */
function resizeComposer() {
  const input = byId('request');
  const transcript = byId('transcript');
  const atEnd = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 70;
  const style = window.getComputedStyle(input);
  const minimum = parseFloat(style.minHeight) || 56;
  const maximum = Math.max(minimum, Math.floor(window.innerHeight * 0.3));
  const border = (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
  input.style.height = '0px';
  const contentHeight = Math.ceil(input.scrollHeight + border);
  input.style.height = `${Math.min(maximum, Math.max(minimum, contentHeight))}px`;
  input.style.overflowY = contentHeight > maximum ? 'auto' : 'hidden';
  if (atEnd) transcript.scrollTop = transcript.scrollHeight;
}

/** Build DOM exclusively through textContent for untrusted tool/source/model values. */
function node(tag, text, className) {
  const element = document.createElement(tag);
  if (text !== undefined) element.textContent = text;
  if (className) element.className = className;
  return element;
}

/** Bind one closed host action without interpolated commands or inline handlers. */
function button(text, action, data = {}) {
  const element = node('button', text);
  const description = action === 'resolve'
    ? (data.value ? 'Approve this pending proposal or allow commands for the current session.' : 'Decline this local approval; remaining requests are not executed.')
    : descriptions[action];
  if (description) describe(element, description);
  element.addEventListener('click', () => send(action, data));
  return element;
}

/** Display a small Markdown subset using paragraph/list/pre elements, never innerHTML. */
function formatted(text) {
  const fragment = document.createDocumentFragment();
  let fence = false;
  let code = [];
  for (const line of String(text).split('\n')) {
    if (line.startsWith('```')) {
      if (fence) { fragment.append(node('pre', code.join('\n'))); code = []; }
      fence = !fence;
    } else if (fence) code.push(line);
    else if (line.trim()) {
      const element = node(line.startsWith('#') ? 'strong' : 'p', line.replace(/^#{1,6}\s*/, ''));
      if (/^[-*]\s/.test(line)) element.className = 'list-line';
      fragment.append(element);
    }
  }
  if (code.length) fragment.append(node('pre', code.join('\n')));
  return fragment;
}

/** Extract bounded source citations; the host validates paths before opening documents. */
function sources(value, result = new Map(), depth = 0) {
  if (!value || typeof value !== 'object' || depth > 8 || result.size >= 8) return result;
  if (typeof value.path === 'string') result.set(value.path, { path: value.path, line: Number.isInteger(value.start) ? value.start : 1 });
  for (const item of Object.values(value)) {
    if (Array.isArray(item)) item.slice(0, 20).forEach(v => sources(v, result, depth + 1));
    else if (item && typeof item === 'object') sources(item, result, depth + 1);
  }
  return result;
}

/** Summarize useful execution facts while retaining exact JSON in expandable details. */
function summary(body) {
  const data = body.data || {};
  if (body.error) return `${body.error.code}: ${body.error.message}`;
  if (data.command_id) return `${data.pending_permission ? 'Command awaiting permission' : `Command ${data.status} · exit ${data.exit_code ?? '?'}`}\n${(data.argv || []).join(' ')}`;
  if (data.patch_id) return `Proposal ${data.patch_id} · ${data.status || 'staged for review'}`;
  if (data.validation) return `Validation ${data.validation.status} · ${data.validation.evidence_kind || 'see evidence'}`;
  if (data.task) return data.task.objective;
  if (data.path) return `${data.path}:${data.start || 1}–${data.end || '?'} · ${String(data.hash || '').slice(0, 10)}`;
  if (Array.isArray(data.hits)) return `${data.hits.length} search matches`;
  if (Array.isArray(data.sources)) return `${data.sources.length} source files · ${data.omissions?.length || 0} omissions`;
  return body.text || body.action || body.next || 'Tool result saved.';
}

/** Render one saved event inside its turn without adding a fragment-level Copy button. */
function card(entry) {
  const body = entry.body;
  const kind = entry.kind;
  const element = node('section', undefined, 'turn-entry ' + kind);
  element.dataset.entryId = entry.id;
  if (kind === 'request' || kind === 'result') element.append(node('strong', kind === 'request' ? 'Tool call' : 'Tool result', 'event-label'));
  if (kind === 'request') {
    const details = node('details'); details.append(node('summary', body.request_id || 'Tool request'), node('pre', body.text)); element.append(details);
  } else {
    const oldBusyRefresh = body.action === 'operation_failed' && body.method === 'exports.prepare' && body.error?.code === 'busy';
    const text = oldBusyRefresh ? 'An earlier context refresh found the repository busy. Use Update context to refresh it now.' : kind === 'result' ? summary(body) : body.text || (body.error ? `${body.error.code || 'Error'}: ${body.error.message || body.error}` : body.action) || 'Local event';
    if (String(text).length > 1100) {
      const details = node('details'); details.append(node('summary', body.action === 'starter' ? 'Session starter · copy into your WebUI' : String(text).slice(0, 120) + '…'), formatted(text)); element.append(details);
    } else element.append(formatted(text));
    if (kind === 'result' || body.reply || body.error) {
      const details = node('details'); details.append(node('summary', 'Details / exact JSON'), node('pre', JSON.stringify(body, null, 2))); element.append(details);
    }
  }
  const controls = node('div', undefined, 'inline-actions');
  const data = body.data || {};
  if (data.command_id && data.files?.length) controls.append(button('Observed changes', 'commandDiff', { commandId: data.command_id }));
  if (data.patch_id && !body.local_action && data.status !== 'applied') controls.append(button('Open diffs', 'review', { patchId: data.patch_id }));
  for (const citation of sources(body).values()) controls.append(button(citation.path, 'source', citation));
  if (controls.childNodes.length) element.append(controls);
  if (body.next) element.append(node('p', body.next, 'hint'));
  if (body.monitoring?.warnings?.length) element.append(node('p', body.monitoring.warnings.join(' '), 'hint'));
  return element;
}

/** Match backend turn hints while keeping older saved/display events readable. */
function turnRole(entry) {
  return entry.turn_role || (['user', 'note'].includes(entry.kind) || entry.body.action === 'forward'
    ? 'user' : entry.kind === 'model' ? 'model' : 'harness');
}

/** Consolidate adjacent harness events and preserve per-message user/model boundaries. */
function groupedTurns(ordered) {
  const turns = [];
  for (const entry of ordered) {
    const role = turnRole(entry);
    const starts = entry.turn_start ?? (['user', 'note', 'model'].includes(entry.kind) ||
      entry.kind === 'local' && entry.body.action === 'session_settings');
    let turn = turns.at(-1);
    if (!turn || turn.role !== role || starts) {
      turn = { id: entry.id, role, entries: [] }; turns.push(turn);
    }
    turn.entries.push(entry);
  }
  return turns;
}

/** Keep one Copy per turn and retain expanded event details when new results arrive. */
function renderTurns(ordered) {
  const container = byId('entries');
  const previous = new Map([...container.children].map(element => [element.dataset.id, element]));
  const eventNodes = new Map([...container.querySelectorAll('[data-entry-id]')].map(element => [element.dataset.entryId, element]));
  const turns = groupedTurns(ordered).map(turn => {
    let element = previous.get(turn.id);
    if (!element?.classList.contains('turn')) {
      element = node('article', undefined, 'message turn ' + turn.role); element.dataset.id = turn.id;
      const head = node('div', undefined, 'message-heading');
      head.append(node('strong', turn.role === 'user' ? 'You' : turn.role === 'model' ? 'WebUI model' : 'RepoPilot'),
        button('Copy', 'copy', { entryId: turn.id }));
      element.append(head, node('div', undefined, 'turn-content'));
    }
    const content = element.querySelector('.turn-content');
    const children = [...content.children];
    if (children.length !== turn.entries.length || turn.entries.some((entry, index) => children[index]?.dataset.entryId !== entry.id)) {
      content.replaceChildren(...turn.entries.map(entry => eventNodes.get(entry.id) || card(entry)));
    }
    return element;
  });
  if (turns.length !== container.children.length || turns.some((element, index) => container.children[index] !== element)) {
    container.replaceChildren(...turns);
  }
}

/** Keep local approval UI separate from any model-provided button or tool text. */
function renderPending(pending) {
  const list = byId('pending'); list.replaceChildren();
  for (const reply of pending || []) {
    const article = node('article', undefined, 'approval');
    const action = reply.pending;
    article.append(node('strong', action.kind === 'patch' ? 'Review source changes' : 'Allow commands for this session?'));
    if (action.kind === 'command') article.append(node('pre', action.argv.join(' ')), node('p', `Working directory: ${action.cwd}`), node('p', 'Commands run with your account permissions and may modify files or use the network. Revoke permission from ⚙ at any time.'));
    else article.append(node('p', 'Native diff tabs show every proposed file. Approval applies the whole proposal and runs the configured validation profile.'));
    article.append(button(action.kind === 'patch' ? 'Approve whole proposal' : 'Allow commands for this session', 'resolve', { replyId: reply.reply_id, value: true }),
      button('Decline', 'resolve', { replyId: reply.reply_id, value: false }));
    list.append(article);
  }
}

/** Restore history and bottom-bar state without overwriting a draft during background refresh. */
function render(message) {
  const changed = session !== message.selected;
  if (changed) { if (!firstRender) saveDraft(); entries.clear(); streams.clear(); }
  session = message.selected; saved.session = session; vscode.setState(saved);
  const choices = byId('sessions'); choices.replaceChildren();
  for (const chat of message.sessions) {
    const row = node('div', undefined, 'session-row');
    row.dataset.sessionId = chat.id;
    const entry = button(chat.name || 'New session', 'select', { sessionId: chat.id });
    entry.className = chat.id === session ? 'session-name selected' : 'session-name';
    const archive = button(chat.archived ? 'Restore' : 'Archive', 'archive', { sessionId: chat.id });
    archive.setAttribute('aria-label', `${chat.archived ? 'Restore' : 'Archive'} ${chat.name || 'New session'}`);
    const remove = button('×', 'delete', { sessionId: chat.id });
    remove.setAttribute('aria-label', `Delete ${chat.name || 'New session'}`);
    describe(archive, chat.archived ? 'Bring this chat back to the active sessions list.' : 'Hide this chat without deleting its history.');
    row.append(entry, archive, remove); choices.append(row);
  }
  if (!session) {
    entries.clear(); streams.clear(); firstRender = true;
    byId('title').textContent = 'RepoPilotV3'; byId('request').value = ''; resizeComposer();
    byId('entries').replaceChildren(node('p', 'Start a new session or select a saved chat. First-time project setup: ⚙ → Copy project prompt; paste it into WebUI project instructions once.', 'empty'));
    for (const id of ['pending', 'items', 'checklist']) byId(id).replaceChildren();
    for (const id of ['packages', 'todo-panel', 'stop']) byId(id).hidden = true;
    byId('todo-toggle').textContent = 'TODO 0/0'; byId('context-meter').textContent = 'Context ~0/40k';
    byId('update-context').textContent = 'Update context'; byId('warning').textContent = '';
    byId('job').textContent = 'Local tools · no connected model';
    return;
  }
  const status = message.status;
  byId('title').textContent = status.session.name || 'New session';
  if (changed || firstRender || !Object.hasOwn(saved.drafts, session)) {
    byId('request').value = Object.hasOwn(saved.drafts, session) ? saved.drafts[session] : status.draft;
    resizeComposer();
    saved.drafts[session] = byId('request').value; vscode.setState(saved);
  }
  firstRender = false;
  byId('mode').value = status.session.mode || 'plan';
  describe(byId('mode'), modes[byId('mode').value]);
  byId('task').textContent = status.task?.objective || 'Waiting for the model to create a task and TODO.';
  const items = status.task?.items || [];
  byId('todo-toggle').textContent = `TODO ${items.filter(i => i.status === 'completed').length}/${items.length}`;
  byId('items').replaceChildren();
  for (const item of items) byId('items').append(node('li', `${item.status === 'completed' ? '☑' : '☐'} ${item.text} · ${item.status} · ${item.reported_by || 'model'} / unverified`));
  const tokens = status.metrics.observed_estimated_tokens;
  const limit = status.metrics.context_limit || 40000;
  byId('context-meter').textContent = `Context ~${Math.round(tokens / 100) / 10}k/${Math.round(limit / 100) / 10}k`;
  describe(byId('context-meter'), `Show recorded token estimates; click to adjust the advisory threshold. ${tokens.toLocaleString()} locally observed estimated tokens. Low confidence; remote usage is unknown. ${status.metrics.exchanges} recorded exchanges.`);
  const warnings = [];
  if (status.session.archived) warnings.push('Archived: resume through ⚙ before continuing.');
  if (status.task?.checkpoint_required) warnings.push('Update TODO after the applied change or failed validation.');
  if (status.interrupted_requests?.length) warnings.push('Interrupted operations were not replayed. Inspect their saved results.');
  if (status.metrics.handoff_recommended) warnings.push('Context threshold reached; a fresh-chat handoff is recommended.');
  byId('warning').textContent = warnings.join(' ');
  for (const entry of message.history || []) entries.set(entry.id, entry);
  const transcript = byId('transcript');
  const atEnd = transcript.scrollHeight - transcript.scrollTop - transcript.clientHeight < 70;
  const ordered = [...entries.values()].sort((a, b) => a.sequence - b.sequence);
  renderTurns(ordered);
  renderPending(status.pending_replies);
  if (changed || atEnd) transcript.scrollTop = transcript.scrollHeight;
  const rows = status.checklist || [];
  const changedCount = rows.filter(r => r.action !== 'keep').length;
  byId('update-context').textContent = `Update context${changedCount ? ' · ' + changedCount : ''}`;
  describe(byId('update-context'), descriptions.prepare + ' ' + (status.package_stale ? 'Source package is stale.' : changedCount ? `${changedCount} artifacts differ from confirmed transfers.` : 'Context package matches confirmed transfers.'));
  byId('freshness').textContent = status.directory ? `${status.package_stale ? 'Refreshing' : 'Current'} · ${status.directory}` : 'Refresh to prepare upload files.';
  byId('checklist').replaceChildren();
  for (const row of rows) byId('checklist').append(node('li', `${row.action.toUpperCase()} ${row.file}${row.required ? '' : ' (optional)'} · ${row.hash.slice(0, 10)}`));
}

/** Toggle one compact surface without creating additional global toolbar rows. */
function toggle(id, control) {
  const panel = byId(id); const wasHidden = panel.hidden;
  for (const other of document.querySelectorAll('.popover')) if (other !== panel) other.hidden = true;
  panel.hidden = !wasHidden;
  if (control) byId(control).setAttribute('aria-expanded', String(!panel.hidden));
}

byId('sessions-toggle').addEventListener('click', () => toggle('session-drawer', 'sessions-toggle'));
byId('options-toggle').addEventListener('click', () => toggle('options', 'options-toggle'));
byId('todo-toggle').addEventListener('click', () => toggle('todo-panel', 'todo-toggle'));
byId('composer-menu').addEventListener('click', () => toggle('composer-options'));
document.querySelectorAll('[data-action]').forEach(element => {
  if (descriptions[element.dataset.action]) describe(element, descriptions[element.dataset.action]);
  element.addEventListener('click', () => {
  const action = element.dataset.action; saveDraft();
  send(action, ['run', 'send', 'note'].includes(action) ? { text: byId('request').value } : {});
  if (action !== 'stop') byId('options').hidden = true;
  byId('composer-options').hidden = true;
  });
});
for (const [id, text] of Object.entries({
  'sessions-toggle': 'Search, select, archive, restore or delete chats.',
  'options-toggle': 'Open session settings, project selection and local tools.',
  'search': 'Search saved sessions by name, identifier or task objective.',
  'archived': 'Display archived sessions so you can restore or delete them.',
  'full': 'Include Markdown exports of all eligible source files.',
  'older': descriptions.history,
  'composer-menu': 'Run the composer as a model reply or save a local note.',
  'todo-toggle': 'Expand the current task checklist.',
  'mode': modes.plan,
  'context-meter': 'Show recorded token estimates; click to adjust the advisory threshold.'
})) describe(byId(id), text);
describe(byId('full').closest('label'), byId('full').title);
describe(byId('archived').closest('label'), byId('archived').title);
for (const option of byId('mode').options) describe(option, modes[option.value]);
byId('mode').addEventListener('change', () => {
  describe(byId('mode'), modes[byId('mode').value]);
  send('mode', { text: byId('mode').value });
});
byId('full').addEventListener('change', () => send('prepare', { value: byId('full').checked }));
byId('older').addEventListener('click', () => send('history'));
byId('request').addEventListener('input', () => { resizeComposer(); clearTimeout(draftTimer); draftTimer = setTimeout(saveDraft, 200); });
window.addEventListener('resize', resizeComposer);
if (document.fonts) void document.fonts.ready.then(resizeComposer);
byId('request').addEventListener('keydown', event => {
  if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
    event.preventDefault(); saveDraft(); send(event.shiftKey ? 'run' : 'send', { text: byId('request').value });
  }
});
function search() { send('search', { text: byId('search').value, value: byId('archived').checked }); }
byId('search').addEventListener('input', () => { clearTimeout(searchTimer); searchTimer = setTimeout(search, 200); });
byId('archived').addEventListener('change', search);
window.addEventListener('message', event => {
  const message = event.data;
  if (message.type === 'workspaceChanged') {
    clearTimeout(draftTimer); session = null; saved.session = null;
    render({ selected: null, sessions: [] });
    projectRoot(message.workspace);
    vscode.setState(saved);
  }
  if (message.type === 'sessionDeleting' || message.type === 'sessionDeleteFailed') {
    const deleting = message.type === 'sessionDeleting';
    for (const row of byId('sessions').children) if (row.dataset.sessionId === message.session) {
      for (const control of row.querySelectorAll('button')) control.disabled = deleting;
    }
    byId('job').textContent = deleting ? 'Deleting session…' : 'Session was kept. See the error for the next step.';
  }
  if (message.type === 'sessionDeleted') {
    for (const row of [...byId('sessions').children]) if (row.dataset.sessionId === message.session) row.remove();
    delete saved.drafts[message.session];
    if (session === message.session) {
      session = null; saved.session = null; clearTimeout(draftTimer);
      render({ selected: null, sessions: [] });
    }
    byId('job').textContent = 'Session deleted.';
    vscode.setState(saved);
  }
  if (message.type === 'clearComposer' && message.session === session && byId('request').value === message.text) {
    byId('request').value = ''; resizeComposer(); saveDraft();
  }
  if (message.type === 'state') render(message);
  if (message.type === 'connection') {
    byId('job').textContent = 'Local harness ready';
    projectRoot(message.workspace);
  }
  if (message.type === 'packages') {
    for (const other of document.querySelectorAll('.popover')) other.hidden = true;
    byId('packages').hidden = false;
  }
  if (message.type === 'error') {
    const article = node('article', undefined, 'message error');
    article.append(node('strong', 'RepoPilot · action needs attention'), node('p', message.message));
    byId('entries').append(article); byId('job').textContent = message.message;
  }
  if (message.type === 'notice') byId('job').textContent = message.message;
  if (message.type === 'progress') {
    byId('job').textContent = message.command_id ? `Command ${message.state}` : `${message.method}: ${message.state}`;
    if (message.command_id) {
      byId('stop').hidden = message.state !== 'running';
      let article = byId('stream-' + message.command_id);
      if (message.state === 'running' && !article) {
        article = node('article', undefined, 'message running'); article.id = 'stream-' + message.command_id;
        const detail = node('details'); detail.append(node('summary', 'Running local command'), node('pre', 'Starting…')); article.append(detail); byId('entries').append(article);
      }
      if (article && message.text) article.querySelector('pre').textContent = message.text;
      if (article && message.state !== 'running') article.remove();
    }
  }
  if (message.type === 'completed' && message.session === session) {
    byId('stop').hidden = true; byId('job').textContent = 'Saved. Copy the harness reply into your WebUI.';
  }
});
resizeComposer();
send('ready');
