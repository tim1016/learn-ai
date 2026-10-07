import { DOCUMENT } from '@angular/common';
import { DestroyRef, computed, effect, inject, linkedSignal, resource, signal, type Signal } from '@angular/core';

import type { ResourceTarget } from '../../../../fleet/resource-target';
import { extractServerMessage } from '../../operation-error';
import {
  BrokerV2PanelService,
  type OptionStructurePreview,
  type OptionStructurePreviewRequest,
} from '../../v2-panel/lib/broker-v2-panel.service';
import { canonicalJson } from '../deploy-draft.store';
import { DEPLOY_TYPING_SETTLE_MS } from '../deploy-money-step.component';

/** One question for the preview: the structure on screen, on one account. */
export interface OptionPreviewAsk {
  readonly target: ResourceTarget;
  readonly body: OptionStructurePreviewRequest;
}

/** What a page shows of its preview. */
export interface OptionPreviewRead {
  /** The answer to exactly what is asked now. `null` before it answers, and once what is asked has changed. */
  readonly answer: Signal<OptionStructurePreview | null>;
  /**
   * An ask the owner is waiting on is settling or on its way: the first for
   * what is asked now, or their own Refresh. An ask the page makes again by
   * itself is not one: the answer on screen stands until the next replaces it.
   */
  readonly asking: Signal<boolean>;
  /** Why the ask got no answer at all, when it did not. */
  readonly failure: Signal<string | null>;
  /** The answer on screen says to ask again, and the page no longer does so by itself: only Refresh asks. */
  readonly stopped: Signal<boolean>;
  /** Ask again now, as the owner's Refresh does. */
  refresh(): void;
}

/** Said when the route gave no answer and no words of its own. Never a preview state: those are the server's. */
export const PREVIEW_FAILED_TEXT = 'The preview could not be read.';

/**
 * How many times a page asks again by itself for one unchanged ask before it
 * stops and leaves it to Refresh. Every ask renews the page's hold on IBKR
 * quote lines for a minute, so a state that never passes (no fresh quote
 * overnight, the feed down) must not be asked for ever. At the one second the
 * server names between asks this is half a minute: three times the ten
 * seconds it allows first quotes to arrive, and as long as Money re-checks a
 * price.
 */
export const PREVIEW_ASK_AGAIN_LIMIT = 30;

/** The shortest wait before the page asks again by itself, whatever wait an answer names. */
export const PREVIEW_ASK_AGAIN_FLOOR_MS = 1_000;

/** A stable name for one page's request for quotes, as the route takes one. */
export function newPreviewKey(): string {
  return crypto.randomUUID();
}

/**
 * One page's asks of the option structure preview (#2913). Call it where a
 * component's fields are made; it stops with that component.
 *
 * It asks once what is asked has stopped changing, and when the owner
 * refreshes. For an answer whose state passes by itself it asks again after
 * the wait the server names, and no sooner than
 * {@link PREVIEW_ASK_AGAIN_FLOOR_MS}: at most {@link PREVIEW_ASK_AGAIN_LIMIT}
 * times for one unchanged ask, and never while the page is hidden. An ask
 * that fell due while it was hidden goes once when it is shown again. A new
 * ask, or a Refresh, starts the count again. An answer that names no wait
 * stays as read. `ask` returns `null` while there is nothing to ask about,
 * and nothing is asked then: a page holds quote lines only while it asks.
 */
export function optionStructurePreview(ask: () => OptionPreviewAsk | null): OptionPreviewRead {
  const service = inject(BrokerV2PanelService);
  const settleMs = inject(DEPLOY_TYPING_SETTLE_MS);
  const page = inject(DOCUMENT);
  const sameAsk = (left: OptionPreviewAsk | null, right: OptionPreviewAsk | null): boolean =>
    canonicalJson(left) === canonicalJson(right);

  const wanted = computed(ask, { equal: sameAsk });
  /** What is asked once typing has paused. */
  const settled = signal<OptionPreviewAsk | null>(null, { equal: sameAsk });
  const read = resource({
    params: () => settled() ?? undefined,
    loader: ({ params }) => service.previewOptionStructure(params.target, params.body),
  });
  const current = computed(() => sameAsk(settled(), wanted()));
  /**
   * The asks the page has made by itself for what is asked now, and whether
   * the one on its way is one of them. It starts again with every new ask.
   */
  const again = linkedSignal({ source: settled, computation: () => ({ asks: 0, onItsWay: false }) });
  const spent = computed(() => again().asks >= PREVIEW_ASK_AGAIN_LIMIT);
  /** The wait the answer in hand names: `null` with no answer, with one on its way, or for a state that does not pass by itself. */
  const namedWait = computed(() => (read.hasValue() && !read.isLoading() ? read.value().ask_again_in_ms : null));

  /** An ask that fell due while the page was hidden, kept for when it is shown again. */
  let owed = false;
  const askAgain = (): void => {
    owed = false;
    again.update(({ asks }) => ({ asks: asks + 1, onItsWay: true }));
    read.reload();
  };
  const askIfOwed = (): void => {
    if (owed && page.visibilityState === 'visible') askAgain();
  };
  page.addEventListener('visibilitychange', askIfOwed);
  inject(DestroyRef).onDestroy(() => page.removeEventListener('visibilitychange', askIfOwed));

  effect((onCleanup) => {
    const next = wanted();
    const timer = setTimeout(() => settled.set(next), settleMs);
    onCleanup(() => clearTimeout(timer));
  });
  effect((onCleanup) => {
    const wait = namedWait();
    if (wait === null || spent()) return;
    const timer = setTimeout(() => {
      if (page.visibilityState === 'visible') askAgain();
      else owed = true;
    }, Math.max(wait, PREVIEW_ASK_AGAIN_FLOOR_MS));
    onCleanup(() => {
      clearTimeout(timer);
      owed = false;
    });
  });

  return {
    answer: computed(() => (current() && read.hasValue() ? read.value() : null)),
    asking: computed(() => wanted() !== null && (!current() || (read.isLoading() && !again().onItsWay))),
    failure: computed(() => {
      const error = current() ? read.error() : undefined;
      return error === undefined ? null : extractServerMessage(error, PREVIEW_FAILED_TEXT);
    }),
    stopped: computed(() => current() && namedWait() !== null && spent()),
    refresh: () => {
      again.set({ asks: 0, onItsWay: false });
      read.reload();
    },
  };
}
