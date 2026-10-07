import { computed, effect, inject, resource, signal, type Signal } from '@angular/core';

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
  /** An ask is settling or on its way. */
  readonly asking: Signal<boolean>;
  /** Why the ask got no answer at all, when it did not. */
  readonly failure: Signal<string | null>;
  /** Ask again now, as the owner's Refresh does. */
  refresh(): void;
}

/** Said when the route gave no answer and no words of its own. Never a preview state: those are the server's. */
export const PREVIEW_FAILED_TEXT = 'The preview could not be read.';

/** A stable name for one page's request for quotes, as the route takes one. */
export function newPreviewKey(): string {
  return crypto.randomUUID();
}

/**
 * One page's asks of the option structure preview (#2913). Call it where a
 * component's fields are made; it stops with that component.
 *
 * It asks once what is asked has stopped changing, when the owner refreshes,
 * and, for an answer whose state passes by itself, again after the wait the
 * server names and no sooner. An answer that names no wait stays as read
 * until one of the first two. `ask` returns `null` while there is nothing to
 * ask about, and nothing is asked then: a page holds quote lines only while
 * it asks.
 */
export function optionStructurePreview(ask: () => OptionPreviewAsk | null): OptionPreviewRead {
  const service = inject(BrokerV2PanelService);
  const settleMs = inject(DEPLOY_TYPING_SETTLE_MS);
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

  effect((onCleanup) => {
    const next = wanted();
    const timer = setTimeout(() => settled.set(next), settleMs);
    onCleanup(() => clearTimeout(timer));
  });
  effect((onCleanup) => {
    const wait = read.hasValue() && !read.isLoading() ? read.value().ask_again_in_ms : null;
    if (wait === null) return;
    const timer = setTimeout(() => read.reload(), wait);
    onCleanup(() => clearTimeout(timer));
  });

  return {
    answer: computed(() => (current() && read.hasValue() ? read.value() : null)),
    asking: computed(() => wanted() !== null && (!current() || read.isLoading())),
    failure: computed(() => {
      const error = current() ? read.error() : undefined;
      return error === undefined ? null : extractServerMessage(error, PREVIEW_FAILED_TEXT);
    }),
    refresh: () => {
      read.reload();
    },
  };
}
