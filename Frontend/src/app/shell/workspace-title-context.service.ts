import { Injectable, signal } from '@angular/core';

/**
 * The one thing the window title needs that the URL cannot supply: the label
 * of the bot whose page is open (ADR 0064 Decision 6 — "Gallery · Paper", and
 * on a bot's page the bot's own name in place of the tab's).
 *
 * A bot's label is server data the shell does not read; only the bot panel
 * holds it. So the panel writes it here while it is open and clears it on
 * destroy, and `AppComponent` — still the only place `Title.setTitle` is ever
 * called — reads it beside the URL-derived state.
 *
 * Deliberately one signal with one meaning, not a page-metadata bus. Anything
 * else a title needs is already in the URL, and the URL is where it should
 * stay: a second writable channel into the title is a second source of truth
 * for what the operator is looking at.
 */
@Injectable({ providedIn: 'root' })
export class WorkspaceTitleContextService {
  private readonly bot = signal<string | null>(null);

  /** The open bot's label, or `null` when no bot page is open. */
  readonly botLabel = this.bot.asReadonly();

  setBotLabel(label: string | null): void {
    this.bot.set(label);
  }
}
