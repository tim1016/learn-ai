import { computed, signal } from '@angular/core';

import { downloadProblemOf, type DownloadProblem } from './past-chain-problems';
import type { DownloadAnswer, DownloadRequest } from './past-chain.types';

/** What a download was asked for: the underlying and the date it was about, and the request as sent. */
export interface DownloadAsked {
  readonly underlying: string;
  readonly tradingDateMs: number;
  readonly request: DownloadRequest;
}

export type DownloadState =
  | { readonly kind: 'idle' }
  | { readonly kind: 'running'; readonly asked: DownloadAsked }
  | { readonly kind: 'answered'; readonly asked: DownloadAsked; readonly answer: DownloadAnswer }
  | { readonly kind: 'failed'; readonly asked: DownloadAsked; readonly problem: DownloadProblem };

/**
 * One download at a time. A press while one is running does nothing, and nothing here cancels a download that is
 * running: the server finishes the captures it has begun whether or not the page is still looking, so a download is
 * never abandoned half way by leaving the tab or changing the date. An answer is kept with what it was asked for, so
 * it is never read as the answer to a different date.
 */
export class ChainDownloadFlow {
  readonly state = signal<DownloadState>({ kind: 'idle' });
  readonly busy = computed(() => this.state().kind === 'running');

  constructor(private readonly send: (request: DownloadRequest) => Promise<DownloadAnswer>) {}

  /**
   * Asks for the download. Resolves to whether what is stored may have changed, so that it should be read again: the
   * server answered, or it may have fetched before the call failed. False when the server refused the request
   * (it did nothing), when the page is not allowed, and when one was already running.
   */
  async run(asked: DownloadAsked): Promise<boolean> {
    if (this.busy()) return false;
    this.state.set({ kind: 'running', asked });
    try {
      const answer = await this.send(asked.request);
      this.state.set({ kind: 'answered', asked, answer });
      return true;
    } catch (error) {
      const problem = downloadProblemOf(error);
      this.state.set({ kind: 'failed', asked, problem });
      return problem.kind === 'refused' && !problem.nothingDownloaded;
    }
  }

  /** Puts away an answer that no longer describes what is on screen. A download that is running is left alone. */
  clear(): void {
    if (!this.busy()) this.state.set({ kind: 'idle' });
  }
}
