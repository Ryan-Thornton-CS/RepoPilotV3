/** Deterministic relay orchestration, isolated from browser tooling and model execution. */
import { createHash } from 'crypto';
import { BrowserPreferences, BrowserSnapshot } from './browser';

/** Browser actions are injected for synthetic tests and native-editor operation. */
export interface RelayIO {
  snapshot(): Promise<BrowserSnapshot>; fill(text: string): Promise<unknown>; send(): Promise<unknown>;
  journal(id: string, phase: string, text?: string): Promise<any>;
  captured(text: string, automatic: boolean): Promise<void>; status(text: string): void;
  current(): boolean;
}

/** Serialize one explicitly armed conversation; never resume a previous mutation. */
export class BrowserRelay {
  public armed = false;
  public working = false;
  private epoch = 0;
  private turns = 0;
  private address = '';
  private seen = new Set<string>();
  private captureInFlight = false;
  private activeIO?: RelayIO;

  /** Stop observation immediately; completed native clicks cannot be undone or replayed. */
  public pause(reason = 'Browser relay paused.'): void {
    this.armed = false; this.epoch++; this.activeIO?.status(reason);
  }

  /** Arm only through a local action and pin the exact current browser route. */
  public async start(io: RelayIO, preferences: BrowserPreferences): Promise<void> {
    this.pause();
    if (!preferences.enabled) throw new Error('Enable Experimental browser relay first.');
    if (!preferences.selectors.input) throw new Error('Select the message input first.');
    if (preferences.mode !== 'fill' && (!preferences.selectors.send || !preferences.selectors.sent || !preferences.selectors.reply || !(preferences.selectors.busy || preferences.selectors.complete))) {
      throw new Error('Sending/capture needs input, Send, sent-message, reply and completion mappings. Run setup.');
    }
    const snapshot = await io.snapshot();
    if (!io.current()) throw new Error('The selected project/session changed.');
    if (snapshot.draft || snapshot.busy) throw new Error('Finish the existing draft/generation before starting the relay.');
    this.address = snapshot.url; this.activeIO = io; this.turns = 0; this.seen.clear();
    this.armed = true; io.status(`Experimental relay running · ${preferences.mode}`);
  }

  private check(io: RelayIO, snapshot?: BrowserSnapshot): void {
    if (!this.armed || !io.current()) throw new Error('Relay is paused or the selected session changed.');
    if (snapshot && snapshot.url !== this.address) throw new Error('Browser navigation changed. Rebind this chat before resuming.');
  }

  /** Transfer one completed response, with durable pre-effect markers and acknowledged sends. */
  public async transfer(io: RelayIO, preferences: BrowserPreferences, id: string, text: string): Promise<void> {
    if (!this.armed || !text) return;
    if (this.working) { this.pause('A second transfer arrived while the browser was busy. Inspect the chat, then Start again.'); return; }
    const epoch = this.epoch;
    this.working = true;
    try {
      this.check(io);
      const previous = await io.journal(id, 'inspect');
      if (previous.phase) throw new Error(`This response already has a ${previous.phase} transfer record. Inspect the browser; automatic replay is disabled.`);
      const before = await io.snapshot(); this.check(io, before);
      if (before.draft || before.busy) throw new Error('The browser has user input or a generation in progress.');
      if (preferences.mode !== 'fill' && this.turns >= preferences.budget) throw new Error('Automatic turn budget reached. Inspect progress and Start to continue.');
      await io.journal(id, 'fill_pending', text);
      this.check(io); await io.fill(text);
      if (epoch !== this.epoch) return;
      await io.journal(id, 'filled', text);
      if (preferences.mode === 'fill') { io.status('Response filled. Click Send in the WebUI, then use Paste + Run for its reply.'); return; }
      this.check(io, await io.snapshot());
      await io.journal(id, 'send_pending', text);
      this.check(io); await io.send();
      let acknowledged = false, generated = false, incompletePolls = 0;
      for (let attempt = 0; attempt < 300; attempt++) {
        if (epoch !== this.epoch) return;
        const state = await io.snapshot(); this.check(io, state);
        const normalized = (value: string): string => value.replace(/\r\n/g, '\n').trim();
        const newReply = (state.replyCount ?? state.replies.length) > (before.replyCount ?? before.replies.length);
        const newSent = (state.sentCount ?? state.sent.length) > (before.sentCount ?? before.sent.length);
        if (!acknowledged && newSent && state.sent.some(value => normalized(value) === normalized(text))) {
          acknowledged = true; await io.journal(id, 'sent', text);
        }
        generated ||= state.busy;
        if (generated && preferences.selectors.complete && !state.busy && !state.complete && newReply) {
          if (++incompletePolls >= 3) throw new Error('Generation stopped without confirmed completion. Partial text was not imported.');
        } else incompletePolls = 0;
        if (acknowledged && !state.busy && (preferences.selectors.complete ? state.complete : generated) && newReply) {
          const reply = state.replies[state.replies.length - 1];
          const fingerprint = createHash('sha256').update(reply).digest('hex');
          if (!reply.trim() || this.seen.has(fingerprint)) throw new Error('An empty or repeated model reply was observed; inspect it before continuing.');
          const imported = await io.journal('capture-' + fingerprint, 'inspect');
          if (imported.phase) throw new Error('This model reply was already captured. Nothing was replayed.');
          await io.journal('capture-' + fingerprint, 'captured', reply);
          this.seen.add(fingerprint); this.turns++;
          // Release the browser slot before the importer can produce its next response.
          this.working = false;
          if (this.captureInFlight) throw new Error('Another captured reply is still being imported.');
          this.captureInFlight = true;
          try { await io.captured(reply, preferences.mode === 'loop'); }
          finally { this.captureInFlight = false; }
          return;
        }
        if (!acknowledged && attempt >= 15) throw new Error('Send acknowledgement is uncertain. Inspect the browser; do not resend blindly.');
        await new Promise(resolve => setTimeout(resolve, 500));
      }
      throw new Error('No confirmed completed reply was observed. Inspect the chat before resuming.');
    } catch (error) { this.pause(String(error)); throw error; }
    finally { this.working = false; }
  }
}
