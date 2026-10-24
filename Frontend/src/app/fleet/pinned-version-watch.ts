import { DOCUMENT } from '@angular/common';
import { DestroyRef, computed, inject, resource, type Signal } from '@angular/core';

import type { WorkspaceAction } from '../shared/workspace/workspace-action';

/** How often a version still waiting for an account is looked at again while the page is open. */
export const PINNED_VERSION_RECHECK_MS = 15_000;

/** The one way out of a refusal: ask again. The watch handles it (`PinnedVersionWatch.onAction`). */
export const PINNED_VERSION_RECHECK_ACTION: WorkspaceAction = { id: 'recheck', label: 'Check again' };

/**
 * What the watch has found about the version the address names. Each phase
 * carries the facts a panel needs to word it; the wording stays with the panel.
 *
 * - `reading`: the first answer has not arrived.
 * - `invalid`: the address names no version at all; nothing is read.
 * - `failed`: the read did not answer.
 * - `substituted`: the answer names another version, so it is not used.
 * - `unavailable`: the answer is about the version asked for, and it is not ready.
 * - `ready`: the answer is about the version asked for, and it is ready.
 */
export type PinnedVersionPhase<TVersion> =
  | { readonly kind: 'reading' }
  | { readonly kind: 'invalid' }
  | { readonly kind: 'failed'; readonly failure: unknown }
  | { readonly kind: 'substituted'; readonly version: TVersion; readonly answeredId: string }
  | { readonly kind: 'unavailable'; readonly version: TVersion }
  | { readonly kind: 'ready'; readonly version: TVersion };

/** How one kind of version is named, read and judged. The only thing a panel supplies besides its wording. */
export interface PinnedVersionSource<TVersion> {
  /** The id as the address carries it, valid or not. */
  readonly id: () => string;
  /** Whether the value is shaped like a version id; anything else is `invalid` and is never read. */
  readonly isValidId: (id: string) => boolean;
  /** Reads the version by its id. */
  readonly read: (id: string) => Promise<TVersion>;
  /** The id the answer says it is about. */
  readonly answeredId: (version: TVersion) => string;
  /** Whether the answer says the version can be deployed. */
  readonly isReady: (version: TVersion) => boolean;
}

export interface PinnedVersionWatch<TVersion> {
  readonly phase: Signal<PinnedVersionPhase<TVersion>>;
  /** Reads again now, unless a read is already under way. The only way a refusal is lifted. */
  recheck(): void;
  /** Hands the watch the clicks that are its own: `PINNED_VERSION_RECHECK_ACTION` asks again. */
  onAction(actionId: string): void;
}

/**
 * Watches the one version a handoff carries to Deploy while the owner chooses an
 * account (PRD #3490 R19 and R23, I02). The signal and options panels differ in
 * what they call the version and which fields they show; how they keep it is this.
 *
 * - It reads exactly the id the address names, and an id that is not shaped like
 *   one is never read.
 * - It looks again every `PINNED_VERSION_RECHECK_MS` while the tab is visible,
 *   because a version waiting for an account can be revoked or changed.
 * - A refusal is sticky: once the phase is anything but `reading` or `ready`, no
 *   timer lifts it, and only `recheck` asks again.
 * - It never substitutes: an answer about another id is a `substituted` phase and
 *   is not offered as the version, and no other id is ever read in its place.
 *
 * It only reads. Call it in an injection context; the timer ends with the host.
 */
export function pinnedVersionWatch<TVersion>(source: PinnedVersionSource<TVersion>): PinnedVersionWatch<TVersion> {
  const document = inject(DOCUMENT);

  const answer = resource({
    params: () => {
      const id = source.id();
      return source.isValidId(id) ? id : undefined;
    },
    loader: ({ params }) => source.read(params),
  });

  const phase = computed<PinnedVersionPhase<TVersion>>(() => {
    const id = source.id();
    if (!source.isValidId(id)) return { kind: 'invalid' };
    const failure: unknown = answer.error();
    if (failure !== undefined) return { kind: 'failed', failure };
    if (!answer.hasValue()) return { kind: 'reading' };
    const version = answer.value();
    const answeredId = source.answeredId(version);
    if (answeredId !== id) return { kind: 'substituted', version, answeredId };
    return source.isReady(version) ? { kind: 'ready', version } : { kind: 'unavailable', version };
  });

  const recheck = (): void => {
    if (!answer.isLoading()) answer.reload();
  };

  const timer = setInterval(() => {
    if (document.visibilityState !== 'visible') return;
    const { kind } = phase();
    if (kind !== 'reading' && kind !== 'ready') return;
    recheck();
  }, PINNED_VERSION_RECHECK_MS);
  inject(DestroyRef).onDestroy(() => clearInterval(timer));

  return {
    phase,
    recheck,
    onAction: (actionId) => {
      if (actionId === PINNED_VERSION_RECHECK_ACTION.id) recheck();
    },
  };
}
