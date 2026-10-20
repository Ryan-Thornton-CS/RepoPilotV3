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
  mcpEnable: 'Register the selected project’s bundled MCP server with VS Code; existing entries are checked first.',
  mcpDisable: 'Disable a selected project registration without redirecting other chats.',
  mcpStatus: 'Inspect registered project roots and MCP API availability; VS Code owns actual connection state.',
  mcpConfig: 'Inspect the existing or proposed bundled MCP launch configuration.',
  generateHarness: 'Preview and generate agents, project instructions and Python hooks for a project or enclosing workspace.',
  harnessWorkspace: 'Open the selected project’s existing .github folder.',
  harnessRecover: 'Restore a failed harness update while preserving later developer edits.',
  retention: 'Edit automatic export/cache size and retention settings in private configuration.',
  projectPrompt: 'Copy reusable project behavior and tool instructions (up to 8,000 characters). Paste into WebUI project settings.',
  starter: 'Copy this chat’s introduction and task state. Project instructions are separate.',
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
  send: 'Run detected tool requests, or prepare ordinary prose for the WebUI.',
  forward: 'Copy your message for the WebUI without executing JSON examples.',
  roleCopy: 'Copy the current project’s saved Markdown role prompt without changing permissions.',
  roleEdit: 'Edit a project-owned prompt file; save before copying.',
  roleReset: 'Restore shipped guidance after confirmation, keeping a recovery copy.',
  todoReset: 'Archive this shared task’s checklist and require a replacement; retain evidence.',
  projectAdd: 'Add a folder as an independent project space.',
  projectRename: 'Rename the project listing without moving its folder.',
  projectRemove: 'Remove this listing; retain folders, sessions, private state and MCP.',
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
  contextFile: 'Open this exact published context artifact in the editor.',
  source: 'Open this cited source file at its referenced line.'
});

/** Provide native hover help and an accessible description without interpreting markup. */
function describe(element, text) {
  element.title = text;
  element.setAttribute('aria-description', text);
}

/** Show the repository boundary in the options menu and its folder-picker tooltip. */
function projectRoot(workspace) {
  byId('workspace-root').textContent = workspace || 'No project selected';
  describe(document.querySelector('[data-action="projectAdd"]'), descriptions.workspace + (workspace ? ` Current project: ${workspace}` : ''));
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

/** Render observed estimates against the configured budget; remote model capacity is unknown. */
function contextMeter(metrics = {}, active = false) {
  const tokens = Number.isFinite(metrics.observed_estimated_tokens) ? Math.max(0, metrics.observed_estimated_tokens) : 0;
  const limit = Number.isFinite(metrics.context_limit) && metrics.context_limit > 0 ? metrics.context_limit : 40000;
  const percent = Math.round(tokens / limit * 1000) / 10;
  const meter = byId('context-meter');
  const label = `Context window: ${percent}% full\n${tokens.toLocaleString()}/${limit.toLocaleString()} tokens used`;
  const tooltip = label + '\nEstimated recorded content against your configured advisory limit; actual remote usage is unknown.' +
    (active ? '\nClick to change the limit.' : '\nSelect a session to track context.');
  meter.disabled = !active;
  meter.classList.toggle('context-full', tokens >= limit);
  meter.dataset.percent = String(percent);
  meter.setAttribute('aria-label', tooltip);
  byId('context-description').textContent = label;
  byId('context-progress').setAttribute('stroke-dashoffset', String(100 - Math.min(100, percent)));
  byId('context-progress').setAttribute('stroke-opacity', tokens > 0 ? '1' : '0');
  describe(meter, tooltip);
}

/** Restore history and bottom-bar state without overwriting a draft during background refresh. */
function render(message) {
  const changed = session !== message.selected;
  if (changed) { if (!firstRender) saveDraft(); entries.clear(); streams.clear(); closeMenus(); }
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
    byId('context-files').replaceChildren(); byId('context-freshness').textContent = 'Select a session to view its package.';
    for (const id of ['pending', 'items', 'checklist']) byId(id).replaceChildren();
    closeMenus(); byId('stop').hidden = true;
    byId('todo-toggle').textContent = 'TODO 0/0'; contextMeter();
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
  byId('task').textContent = status.task?.objective || 'Waiting for the model to create a task and TODO.';
  const items = status.task?.items || [];
  byId('todo-toggle').textContent = `TODO ${items.filter(i => i.status === 'completed').length}/${items.length}`;
  byId('items').replaceChildren();
  for (const item of items) byId('items').append(node('li', `${item.status === 'completed' ? '☑' : '☐'} ${item.text} · ${item.status} · ${item.reported_by || 'model'} / unverified`));
  contextMeter(status.metrics, true);
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
  byId('context-files').replaceChildren();
  byId('context-freshness').textContent = status.directory ? `${status.package_stale ? 'STALE — refresh before uploading' : 'Current package'} · ${status.directory}` : 'Refresh to generate this session’s context files.';
  for (const row of rows) {
    byId('checklist').append(node('li', `${row.action.toUpperCase()} ${row.file}${row.required ? '' : ' (optional)'} · ${row.hash.slice(0, 10)}`));
    const entry = node('article', undefined, 'context-file');
    entry.append(node('strong', row.file), node('p', `${row.action.toUpperCase()} · ${row.hash.slice(0, 10)}${row.required ? '' : ' · optional'}`), button('Open file', 'contextFile', { path: row.file }));
    byId('context-files').append(entry);
  }
}

const menuControls = {
  options: 'options-toggle', 'session-drawer': 'sessions-toggle',
  'composer-options': 'composer-menu', 'todo-panel': 'todo-toggle', packages: 'update-context'
};
let menuTrigger;

/** Close transient menus consistently, preserving the initiating button for Escape focus. */
function closeMenus(restoreFocus = false) {
  const open = Object.keys(menuControls).some(id => !byId(id).hidden);
  for (const [id, control] of Object.entries(menuControls)) {
    byId(id).hidden = true;
    byId(control).setAttribute('aria-expanded', 'false');
  }
  if (open && restoreFocus && menuTrigger) byId(menuTrigger).focus();
  return open;
}

/** Constrain settings to the space beneath its actual cogwheel, including resized layouts. */
function sizeSettings() {
  if (byId('options').hidden) return;
  const bounds = byId('options-toggle').getBoundingClientRect();
  const available = Math.max(0, window.innerHeight - bounds.bottom - 12);
  byId('options').style.maxWidth = `${Math.max(0, (Number.isFinite(bounds.right) ? bounds.right : window.innerWidth) - 8)}px`;
  byId('options').style.maxHeight = `${Math.min(window.innerHeight * 0.6, available)}px`;
}

/** Open one anchored menu and update its accessible disclosure state. */
function openMenu(id, control = menuControls[id]) {
  closeMenus();
  byId(id).hidden = false;
  menuTrigger = control;
  byId(control).setAttribute('aria-expanded', 'true');
  sizeSettings();
}

/** Toggle one compact surface without covering its own initiating control. */
function toggle(id, control = menuControls[id]) {
  const wasHidden = byId(id).hidden;
  closeMenus();
  if (wasHidden) openMenu(id, control);
}

// Escape dismisses custom menus only; it never cancels work or submits the composer.
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && closeMenus(true)) {
    event.preventDefault(); event.stopPropagation();
  }
});
document.addEventListener('click', event => {
  const within = Object.entries(menuControls).some(([id, control]) => !byId(id).hidden &&
    (byId(id).contains(event.target) || byId(control).contains(event.target)));
  if (!within) closeMenus();
});
window.addEventListener('resize', sizeSettings);
if (window.ResizeObserver) new ResizeObserver(sizeSettings).observe(byId('options-anchor'));

/** Switch accessible tab groups without executing requests or copying content. */
function tabs(group, selected, panels) {
  closeMenus();
  for (const [buttonId, panelId] of panels) {
    byId(buttonId).setAttribute('aria-selected', String(buttonId === selected));
    byId(buttonId).tabIndex = buttonId === selected ? 0 : -1;
    byId(panelId).hidden = buttonId !== selected;
  }
  saved[group] = selected; vscode.setState(saved);
}
const topPages = [['projects-tab', 'projects-page'], ['documentation-tab', 'documentation-page']];
const projectPages = [['sessions-tab', 'session-page'], ['context-tab', 'context-page']];
for (const [group, pages] of [['topPage', topPages], ['projectPage', projectPages]]) {
  for (const [id] of pages) {
    byId(id).addEventListener('click', () => tabs(group, id, pages));
    byId(id).addEventListener('keydown', event => {
      if (['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) {
        event.preventDefault();
        const next = event.key === 'Home' ? pages[0][0] : event.key === 'End' ? pages.at(-1)[0] : pages.find(([other]) => other !== id)[0];
        tabs(group, next, pages); byId(next).focus();
      }
    });
  }
  tabs(group, pages.some(([id]) => id === saved[group]) ? saved[group] : pages[0][0], pages);
}
byId('project-select').addEventListener('change', () => {
  saveDraft(); send('projectSelect', { projectId: byId('project-select').value });
});
let helpTopics = [];
/** Render trusted bundled help through text nodes, never Markdown HTML or source markup. */
function help() {
  const query = byId('help-search').value.toLowerCase();
  const container = byId('help-topics'); container.replaceChildren();
  for (const topic of helpTopics.filter(t => (t.title + ' ' + t.text).toLowerCase().includes(query))) {
    const article = node('article', undefined, 'help-topic');
    article.append(node('h2', topic.title), formatted(topic.text));
    if (topic.image) {
      const image = node('img'); image.src = topic.image; image.alt = topic.alt || topic.title; image.loading = 'lazy'; article.append(image);
    }
    container.append(article);
  }
  if (!container.childNodes.length) container.append(node('p', 'No matching help topics.'));
}
byId('help-search').addEventListener('input', help);

byId('sessions-toggle').addEventListener('click', () => toggle('session-drawer', 'sessions-toggle'));
byId('options-toggle').addEventListener('click', () => toggle('options', 'options-toggle'));
byId('todo-toggle').addEventListener('click', () => toggle('todo-panel', 'todo-toggle'));
byId('composer-menu').addEventListener('click', () => toggle('composer-options', 'composer-menu'));
document.querySelectorAll('[data-action]').forEach(element => {
  if (descriptions[element.dataset.action]) describe(element, descriptions[element.dataset.action]);
  element.addEventListener('click', () => {
  const action = element.dataset.action; saveDraft();
  send(action, action === 'roleCopy' ? { text: element.dataset.role } : ['run', 'send', 'forward', 'note'].includes(action) ? { text: byId('request').value } : {});
  if (action !== 'stop') closeMenus();
  });
});
for (const [id, text] of Object.entries({
  'sessions-toggle': 'Search, select, archive, restore or delete chats.',
  'options-toggle': 'Open grouped session, context, Copilot/MCP, project and maintenance settings.',
  'search': 'Search saved sessions by name, identifier or task objective.',
  'archived': 'Display archived sessions so you can restore or delete them.',
  'full': 'Include Markdown exports of all eligible source files.',
  'older': descriptions.history,
  'composer-menu': 'Run the composer as a model reply or save a local note.',
  'todo-toggle': 'Expand the current task checklist.',
  'context-meter': 'Show recorded token estimates; click to adjust the advisory threshold.'
})) describe(byId(id), text);
describe(byId('full').closest('label'), byId('full').title);
describe(byId('archived').closest('label'), byId('archived').title);
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
  if (message.type === 'documentation') { helpTopics = message.topics; help(); }
  if (message.type === 'projects') {
    const select = byId('project-select'); select.replaceChildren();
    if (!message.projects.length) { const empty = node('option', 'Add a project folder'); empty.value = ''; select.append(empty); }
    for (const project of message.projects) { const option = node('option', project.name); option.value = project.id; option.selected = project.uri === message.selectedUri; option.title = project.uri; select.append(option); }
    if (!message.selectedUri && message.projects.length) { const placeholder = node('option', 'Select project'); placeholder.value = ''; placeholder.selected = true; select.prepend(placeholder); }
  }
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
    openMenu('packages');
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
contextMeter();
send('ready');
