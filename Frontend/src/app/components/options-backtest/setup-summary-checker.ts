import { signal } from '@angular/core';

import { extractServerMessage } from '../broker/operation-error';
import { SetupInvalidError } from './options-backtest.service';
import type { SetupRequest, SetupSummary } from './options-backtest.types';
import { problemsFromDetails, type SetupProblems } from './setup-problems';

/** What the server made of the setup the page holds now. */
export type SummaryState =
  | { readonly kind: 'idle' }
  | { readonly kind: 'checking' }
  | { readonly kind: 'ready'; readonly summary: SetupSummary; readonly body: SetupRequest }
  | { readonly kind: 'invalid'; readonly problems: SetupProblems }
  | { readonly kind: 'failed'; readonly message: string };

const IDLE: SummaryState = { kind: 'idle' };
const CHECKING: SummaryState = { kind: 'checking' };

/**
 * Reads the setup back to the owner in plain sentences, after a pause in the
 * editing; a `ready` answer carries the body it read, so that what is saved or
 * exported can be checked to be exactly that body. Every request carries a generation, and an answer lands only if no
 * newer request or clear has started since: a slow reply to an earlier setup
 * can never overwrite the sentences of the setup on screen.
 */
export class SetupSummaryChecker {
  private readonly current = signal<SummaryState>(IDLE);
  readonly state = this.current.asReadonly();

  private generation = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private shown: string | null = null;
  private lastBody: SetupRequest | null = null;

  constructor(
    private readonly describe: (body: unknown) => Promise<SetupSummary>,
    private readonly debounceMs: () => number,
  ) {}

  /** Read this body back; `null` when the page holds no body to read (the setup is not complete). */
  show(body: SetupRequest | null): void {
    const sameAsShown = body !== null && JSON.stringify(body) === this.shown && this.current().kind !== 'idle';
    if (sameAsShown) return;
    this.clearTimer();
    this.generation += 1;
    this.lastBody = body;
    this.shown = body === null ? null : JSON.stringify(body);
    if (body === null) {
      this.current.set(IDLE);
      return;
    }
    this.current.set(CHECKING);
    this.schedule(body, this.generation);
  }

  /** Ask again for the body last shown, e.g. after a read that failed. */
  retry(): void {
    const body = this.lastBody;
    if (body === null) return;
    this.clearTimer();
    this.generation += 1;
    this.current.set(CHECKING);
    void this.run(body, this.generation);
  }

  /** No answer arrives after this. */
  stop(): void {
    this.clearTimer();
    this.generation += 1;
  }

  private schedule(body: SetupRequest, generation: number): void {
    const wait = this.debounceMs();
    if (wait <= 0) void this.run(body, generation);
    else this.timer = setTimeout(() => void this.run(body, generation), wait);
  }

  private clearTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  private async run(body: SetupRequest, generation: number): Promise<void> {
    let next: SummaryState;
    try {
      next = { kind: 'ready', summary: await this.describe(body), body };
    } catch (error) {
      next =
        error instanceof SetupInvalidError
          ? { kind: 'invalid', problems: problemsFromDetails(error.details, body.legs.map((leg) => leg.leg_id)) }
          : { kind: 'failed', message: extractServerMessage(error, 'The setup could not be read back. Try again.') };
    }
    if (generation === this.generation) this.current.set(next);
  }
}
