import { DOCUMENT } from '@angular/common';
import { DestroyRef, effect, inject } from '@angular/core';

/**
 * How many times a page asks again by itself for one unchanged ask before it
 * stops and leaves it to the owner's own refresh. Every ask about an option
 * structure renews the page's hold on IBKR quote lines for a minute, so a
 * state that never passes (no fresh quote overnight, the feed down) must not
 * be asked for ever. At the one second the server names between asks this is
 * half a minute: three times the ten seconds it allows first quotes to
 * arrive.
 */
export const ASK_AGAIN_LIMIT = 30;

/** The shortest wait before a page asks again by itself, whatever wait an answer names. */
export const ASK_AGAIN_FLOOR_MS = 1_000;

/** The longest. A hold on quote lines lasts a minute, so a slower ask than this would let it lapse. */
export const ASK_AGAIN_CEILING_MS = 30_000;

/**
 * The wait before a page asks again by itself: the one the answer names,
 * held between the floor and the ceiling. A wait that is no ordinary number
 * is the ceiling, so a wait the page cannot read is never a burst of asks.
 */
export function askAgainWaitMs(named: number): number {
  return Number.isFinite(named) ? Math.min(Math.max(named, ASK_AGAIN_FLOOR_MS), ASK_AGAIN_CEILING_MS) : ASK_AGAIN_CEILING_MS;
}

/** What one page's asking again is told. */
export interface AskAgain {
  /** The wait the answer in hand names: `null` with no answer, with an ask on its way, or for an answer that names none. */
  readonly wait: () => number | null;
  /** The page has made every ask it makes by itself for what is asked now. */
  readonly spent: () => boolean;
  /** Ask again, and count it. */
  readonly ask: () => void;
}

/**
 * Asks again after the wait an answer names (see {@link askAgainWaitMs}),
 * until the caller says its asks are spent, and never while the page is
 * hidden: an ask that fell due then goes once when the page is shown again,
 * and is counted like any other. Call it where a component's fields are
 * made; it stops with that component.
 */
export function askAgainWhileShown({ wait, spent, ask }: AskAgain): void {
  const page = inject(DOCUMENT);
  /** An ask that fell due while the page was hidden, kept for when it is shown again. */
  let owed = false;
  const askNow = (): void => {
    owed = false;
    ask();
  };
  const askIfOwed = (): void => {
    if (owed && page.visibilityState === 'visible') askNow();
  };
  page.addEventListener('visibilitychange', askIfOwed);
  inject(DestroyRef).onDestroy(() => page.removeEventListener('visibilitychange', askIfOwed));

  effect((onCleanup) => {
    const named = wait();
    if (named === null || spent()) return;
    const timer = setTimeout(() => {
      if (page.visibilityState === 'visible') askNow();
      else owed = true;
    }, askAgainWaitMs(named));
    onCleanup(() => {
      clearTimeout(timer);
      owed = false;
    });
  });
}
