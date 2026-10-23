/** Fixed browser operations using native tools, without model calls or proposed APIs. */
export type RelayMode = 'fill' | 'capture' | 'loop';

/** Private, user-picked mappings; these are data, never executable scripts. */
export interface BrowserMappings {
  input?: string; send?: string; reply?: string; sent?: string; busy?: string; complete?: string;
}

/** Settings persist, while live tab bindings and running state deliberately do not. */
export interface BrowserPreferences {
  enabled: boolean; mode: RelayMode; budget: number; url: string; selectors: BrowserMappings;
}

/** A browser snapshot contains only selected controls and message regions. */
export interface BrowserSnapshot {
  url: string; draft: string; busy: boolean; replies: string[]; sent: string[]; complete: boolean;
  replyCount?: number; sentCount?: number;
}

/** An explicit timeout rejects uncertain effects rather than retrying them. */
export async function bounded<T>(promise: Promise<T>, milliseconds = 15000): Promise<T> {
  let timer: ReturnType<typeof setTimeout> | undefined;
  try { return await Promise.race([promise, new Promise<T>((_, reject) => { timer = setTimeout(() => reject(new Error('Browser operation timed out. Its effects may be uncertain; inspect the page before resuming.')), milliseconds); })]); }
  finally { if (timer) clearTimeout(timer); }
}

/** Inject only this fixed picker, then read its result using the native browser tool. */
export function installPicker(data: { token: string; family: boolean }): boolean {
  const w = window as any;
  w.__repopilotPicker?.cancel?.();
  const outline = document.createElement('div');
  outline.setAttribute('aria-hidden', 'true');
  Object.assign(outline.style, { position: 'fixed', pointerEvents: 'none', border: '3px solid #54baff', background: '#54baff22', zIndex: '2147483647' });
  document.documentElement.append(outline);
  const state: any = { token: data.token, done: false };
  /** Prefer semantic family attributes; positional paths are acceptable only for single controls. */
  const selector = (element: Element): string => {
    const tag = element.tagName.toLowerCase();
    for (const attr of ['data-testid', 'data-role', 'aria-label']) {
      const value = element.getAttribute(attr);
      if (value) return `${tag}[${attr}=${JSON.stringify(value)}]`;
    }
    if (!data.family && element.id) return '#' + CSS.escape(element.id);
    const classes = [...element.classList].filter(c => /^[a-zA-Z_-][\w-]*$/.test(c) && !/\d{4}/.test(c));
    if (classes.length) return tag + classes.map(c => '.' + CSS.escape(c)).join('');
    if (data.family) throw new Error('Select a message bubble with a stable class or data/ARIA label, or edit its selector manually.');
    const parts = []; let current: Element | null = element;
    while (current && current !== document.documentElement && parts.length < 12) {
      const parent: Element | null = current.parentElement;
      const siblings: Element[] = parent ? [...parent.children].filter(e => e.tagName === current!.tagName) : [];
      parts.unshift(current.tagName.toLowerCase() + ':nth-of-type(' + (siblings.indexOf(current) + 1) + ')'); current = parent;
    }
    return parts.join(' > ');
  };
  const cleanup = (): void => { outline.remove(); document.removeEventListener('mousemove', move, true); document.removeEventListener('click', click, true); document.removeEventListener('keydown', key, true); };
  const move = (event: MouseEvent): void => {
    const target = event.target as Element; if (!(target instanceof Element)) return;
    const r = target.getBoundingClientRect(); Object.assign(outline.style, { left: r.left + 'px', top: r.top + 'px', width: r.width + 'px', height: r.height + 'px' });
  };
  const click = (event: MouseEvent): void => {
    event.preventDefault(); event.stopImmediatePropagation();
    try {
      const element = event.target as Element;
      if (element.tagName === 'IFRAME') throw new Error('This picker supports the main document. A chat inside an iframe needs a different adapter; use copy/paste.');
      state.selector = selector(element); state.preview = (element.textContent || element.getAttribute('placeholder') || element.tagName).slice(0, 160);
      state.matches = document.querySelectorAll(state.selector).length; state.frame = location.href;
      if (element.hasAttribute('data-complete')) state.complete = state.selector + '[data-complete="true"]';
    } catch (error) { state.error = String(error); }
    state.done = true; cleanup();
  };
  const key = (event: KeyboardEvent): void => { if (event.key === 'Escape') { event.preventDefault(); event.stopImmediatePropagation(); state.done = true; state.cancelled = true; cleanup(); } };
  state.cancel = (): void => { state.done = true; state.cancelled = true; cleanup(); };
  w.__repopilotPicker = state;
  document.addEventListener('mousemove', move, true); document.addEventListener('click', click, true); document.addEventListener('keydown', key, true);
  return true;
}

/** Read selected text and explicit completion signals; never infer completion from silence. */
export function inspectPage(selectors: BrowserMappings): BrowserSnapshot {
  const all = (selector?: string): Element[] => selector ? [...document.querySelectorAll(selector)] : [];
  const visible = (e: Element): boolean => !!(e.getClientRects().length && getComputedStyle(e).visibility !== 'hidden');
  const inputs = all(selectors.input);
  if (inputs.length !== 1) throw new Error('The input mapping must match exactly one element. Run setup again.');
  const input = inputs[0] as HTMLTextAreaElement;
  const replies = all(selectors.reply).filter(visible);
  return { url: location.href, draft: typeof input.value === 'string' ? input.value : input.textContent || '',
    busy: all(selectors.busy).some(e => visible(e) && !(e as HTMLButtonElement).disabled),
    complete: !!selectors.complete && all(selectors.complete).some(e => visible(e) && e === replies[replies.length - 1]),
    replyCount: replies.length, sentCount: all(selectors.sent).length,
    replies: replies.slice(-3).map(e => (e.textContent || '').slice(0, 2500000)),
    sent: all(selectors.sent).slice(-3).map(e => (e.textContent || '').slice(0, 2500000)) };
}

/** Only tools implementing the known native browser schema are eligible. */
export class NativeBrowser {
  private executeName?: string;
  private listName?: string;
  private pickerPage?: string;
  /** Accept the editor API at runtime so ordinary tests/older editors need no browser imports. */
  constructor(private api: any) {}

  /** Feature-detect public tool invocation without triggering network or browser actions. */
  public available(): boolean {
    const tools = this.api.lm?.tools ?? [];
    this.executeName = tools.find((t: any) => ['run_playwright_code', 'runPlaywrightCode'].includes(t.name) && t.inputSchema?.properties?.pageId && t.inputSchema?.properties?.code)?.name;
    this.listName = tools.find((t: any) => ['list_browser_pages', 'listBrowserPages'].includes(t.name))?.name;
    return !!(this.api.lm?.invokeTool && this.executeName && this.listName);
  }

  private async invoke(name: string, input: object): Promise<string> {
    const cancel = this.api.CancellationTokenSource ? new this.api.CancellationTokenSource() : undefined;
    try {
      const result: any = await bounded(this.api.lm.invokeTool(name, { input, toolInvocationToken: undefined }, cancel?.token));
      const text = (result.content || []).filter((p: any) => typeof p.value === 'string').map((p: any) => p.value).join('\n');
      if (result.isError || result.toolResultError) throw new Error(text || 'Native browser tool failed.');
      return text;
    } finally { cancel?.cancel(); cancel?.dispose(); }
  }

  /** List pages only when the user explicitly requests browser setup. */
  public async pages(): Promise<{ id: string; label: string; url: string }[]> {
    if (!this.available()) throw new Error('Experimental relay unavailable: enable native Browser tools in a compatible VS Code. Normal copy/paste remains available.');
    const text = await this.invoke(this.listName!, {});
    return [...text.matchAll(/^- \[([^\]]+)\]\s+(.+?)\s+\((https?:\/\/.*?)\)(?:\s|$)/gm)].map(m => ({ id: m[1], label: m[2], url: m[3] }));
  }

  /** Open an explicitly requested tool-owned tab; its login can differ from ordinary tabs. */
  public async open(url: string): Promise<string> {
    if (!this.available()) throw new Error('Native browser control is unavailable.');
    const tool = this.api.lm.tools.find((t: any) => ['open_browser_page', 'openBrowserPage'].includes(t.name) && t.inputSchema?.properties?.url);
    if (!tool) throw new Error('The native browser cannot open a controllable tab in this editor. Use copy/paste.');
    const text = await this.invoke(tool.name, { url, forceNew: true });
    const match = /Page ID:\s*(\S+)/.exec(text);
    if (!match) throw new Error('Browser did not return a confirmed page identity. Inspect the tab before retrying.');
    return match[1];
  }

  /** Detach the temporary picker without altering the page's own click handlers. */
  public async cancelPicker(): Promise<void> {
    const page = this.pickerPage;
    if (page) await this.evaluate(page, () => { (window as any).__repopilotPicker?.cancel?.(); return true; }, null).catch(() => {});
  }

  /** Execute fixed local code through the host's own Playwright permission controls. */
  public async code<T>(pageId: string, code: string): Promise<T> {
    if (!this.available()) throw new Error('Native browser tools are unavailable.');
    let text = await this.invoke(this.executeName!, { pageId, code, timeoutMs: 5000 });
    const deadline = Date.now() + 30000;
    for (;;) {
      const deferred = /\[deferredResultId=([^\]]+)\]/.exec(text);
      if (!deferred) break;
      if (Date.now() >= deadline) throw new Error('Browser action is still pending. Inspect its effects; it will not be replayed.');
      // Wait for the existing operation; never submit its mutation code again.
      text = await this.invoke(this.executeName!, { pageId, deferredResultId: deferred[1], timeoutMs: 5000 });
    }
    const result = /^Result: (.+)$/m.exec(text);
    if (!result) throw new Error('Browser returned no confirmed result. Inspect the page; nothing will be replayed. ' + text.slice(0, 1200));
    return JSON.parse(result[1]);
  }

  /** Evaluate a compiled local function with JSON data; no model/site code becomes a command. */
  public evaluate<T>(pageId: string, fn: Function, data: unknown): Promise<T> {
    return this.code(pageId, `return await page.evaluate(${fn.toString()}, ${JSON.stringify(data)});`);
  }

  /** Wait for a user-selected locator; Escape and timeout leave no active picker listeners. */
  public async pick(pageId: string, family = false): Promise<any> {
    this.pickerPage = pageId;
    const token = Math.random().toString(36).slice(2);
    await this.evaluate(pageId, installPicker, { token, family });
    try {
      for (let attempt = 0; attempt < 90; attempt++) {
        const result = await this.evaluate<any>(pageId, () => { const p = (window as any).__repopilotPicker; return p ? { token: p.token, done: p.done, selector: p.selector, preview: p.preview, matches: p.matches, complete: p.complete, error: p.error, cancelled: p.cancelled } : null; }, null);
        if (!result || result.token !== token) throw new Error('Picker was interrupted or the page changed.');
        if (result.done) { if (result.error) throw new Error(result.error); return result.cancelled ? undefined : result; }
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      throw new Error('Picker timed out. Start setup again when ready.');
    } finally { await this.cancelPicker(); this.pickerPage = undefined; }
  }

  /** Snapshot only the regions selected by the developer. */
  public snapshot(pageId: string, selectors: BrowserMappings): Promise<BrowserSnapshot> { return this.evaluate(pageId, inspectPage, selectors); }

  /** Fill only an empty, uniquely selected editable input using framework-compatible events. */
  public fill(pageId: string, selectors: BrowserMappings, text: string, address?: string): Promise<boolean> {
    return this.code(pageId, `if(${JSON.stringify(address || '')} && page.url()!==${JSON.stringify(address || '')})throw new Error('Browser navigation changed');const input=page.locator(${JSON.stringify(selectors.input)}); if(await input.count()!==1)throw new Error('Input mapping is ambiguous'); const draft=await input.evaluate(e=>'value' in e?e.value:e.textContent);if(draft)throw new Error('Input contains user text; relay paused');await input.fill(${JSON.stringify(text)});return true;`);
  }

  /** A single click may have uncertain effects, so callers must never retry it implicitly. */
  public send(pageId: string, selectors: BrowserMappings, address?: string): Promise<boolean> {
    return this.code(pageId, `if(${JSON.stringify(address || '')} && page.url()!==${JSON.stringify(address || '')})throw new Error('Browser navigation changed');const send=page.locator(${JSON.stringify(selectors.send)});if(await send.count()!==1)throw new Error('Send mapping is ambiguous');await send.evaluate(e=>{if(!e.getClientRects().length || getComputedStyle(e).visibility==='hidden' || e.matches(':disabled') || e.getAttribute('aria-disabled')==='true')throw new Error('Send control is hidden or disabled');e.click();});return true;`);
  }
}
