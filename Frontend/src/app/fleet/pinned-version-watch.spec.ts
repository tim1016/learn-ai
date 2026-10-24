import { ApplicationRef, signal } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  PINNED_VERSION_RECHECK_ACTION,
  PINNED_VERSION_RECHECK_MS,
  pinnedVersionWatch,
  type PinnedVersionPhase,
} from './pinned-version-watch';

/**
 * The watch behind both handoff panels on the account list (the signal version and the
 * options version): it polls while the owner chooses an account, holds a refusal by name
 * until they ask again, and never puts another version in the place of the one asked for.
 * The panels' own specs prove their wording; this proves the keeping.
 */

interface Version {
  readonly id: string;
  readonly ready: boolean;
}

const ASKED = 'v-1';
const OTHER = 'v-2';

function ready(id = ASKED): Version {
  return { id, ready: true };
}

function revoked(id = ASKED): Version {
  return { id, ready: false };
}

/** A watch on the id in the returned signal, reading through `read` and judging a version ready by its flag. */
function watching(read: (id: string) => Promise<Version>, id = ASKED) {
  const pinned = signal(id);
  const watch = TestBed.runInInjectionContext(() =>
    pinnedVersionWatch<Version>({
      id: pinned,
      isValidId: (value) => /^v-\d+$/.test(value),
      read,
      answeredId: (version) => version.id,
      isReady: (version) => version.ready,
    }),
  );
  return { pinned, watch };
}

async function settle(): Promise<void> {
  TestBed.tick();
  await TestBed.inject(ApplicationRef).whenStable();
}

/** One poll interval later, with whatever the poll read settled. */
async function pollOnce(): Promise<void> {
  vi.advanceTimersByTime(PINNED_VERSION_RECHECK_MS);
  await settle();
}

function kindOf(phase: PinnedVersionPhase<Version>): string {
  return phase.kind;
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
});

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('pinnedVersionWatch', () => {
  it('reads exactly the version it was given, and says it is reading until that answer arrives', async () => {
    const read = vi.fn(async (id: string) => ready(id));
    const { watch } = watching(read);

    expect(kindOf(watch.phase())).toBe('reading');
    await settle();

    expect(watch.phase()).toEqual({ kind: 'ready', version: ready() });
    expect(read).toHaveBeenCalledTimes(1);
    expect(read).toHaveBeenCalledWith(ASKED);
  });

  it('asks again every interval while the version stays ready, and shows what the new answer says', async () => {
    let answer = ready();
    const read = vi.fn(async () => answer);
    const { watch } = watching(read);
    await settle();

    await pollOnce();
    expect(read).toHaveBeenCalledTimes(2);
    expect(kindOf(watch.phase())).toBe('ready');

    answer = revoked();
    await pollOnce();
    expect(read).toHaveBeenCalledTimes(3);
    expect(watch.phase()).toEqual({ kind: 'unavailable', version: revoked() });
  });

  it('does not ask again while the tab is hidden, and resumes when it is shown', async () => {
    const read = vi.fn(async () => ready());
    const { watch } = watching(read);
    await settle();

    const visibility = vi.spyOn(document, 'visibilityState', 'get');
    visibility.mockReturnValue('hidden');
    await pollOnce();
    await pollOnce();
    expect(read).toHaveBeenCalledTimes(1);

    visibility.mockReturnValue('visible');
    await pollOnce();
    expect(read).toHaveBeenCalledTimes(2);
    expect(kindOf(watch.phase())).toBe('ready');
  });

  it('keeps a refusal until the owner asks again, however long it waits, and lifts it only if that read says ready', async () => {
    let answer = revoked();
    const read = vi.fn(async () => answer);
    const { watch } = watching(read);
    await settle();
    expect(watch.phase()).toEqual({ kind: 'unavailable', version: revoked() });

    // It is ready again elsewhere, but the watch does not quietly turn back on its own.
    answer = ready();
    for (let intervals = 0; intervals < 4; intervals += 1) await pollOnce();
    expect(read).toHaveBeenCalledTimes(1);
    expect(kindOf(watch.phase())).toBe('unavailable');

    watch.onAction(PINNED_VERSION_RECHECK_ACTION.id);
    await settle();
    expect(read).toHaveBeenCalledTimes(2);
    expect(watch.phase()).toEqual({ kind: 'ready', version: ready() });
  });

  it('keeps an unreadable version as a failure with the reason, until the owner asks again', async () => {
    const reason = new Error('The server did not answer.');
    let outcome: Promise<Version> = Promise.reject(reason);
    const read = vi.fn(() => outcome);
    const { watch } = watching(read);
    await settle();
    expect(watch.phase()).toEqual({ kind: 'failed', failure: reason });

    outcome = Promise.resolve(ready());
    await pollOnce();
    await pollOnce();
    expect(read).toHaveBeenCalledTimes(1);
    expect(kindOf(watch.phase())).toBe('failed');

    watch.recheck();
    await settle();
    expect(watch.phase()).toEqual({ kind: 'ready', version: ready() });
  });

  it('never substitutes: an answer about another version is refused, and no other version is ever read in its place', async () => {
    const read = vi.fn(async (_id: string) => ready(OTHER));
    const { watch } = watching(read);
    await settle();

    expect(watch.phase()).toEqual({ kind: 'substituted', version: ready(OTHER), answeredId: OTHER });
    await pollOnce();
    await pollOnce();
    watch.recheck();
    await settle();

    expect(kindOf(watch.phase())).toBe('substituted');
    expect(read).toHaveBeenCalledTimes(2);
    expect(read.mock.calls.every(([id]) => id === ASKED)).toBe(true);
  });

  it('never shows the answer for the old version once the address names another', async () => {
    const read = vi.fn(async (id: string) => ready(id));
    const { pinned, watch } = watching(read);
    await settle();
    expect(watch.phase()).toEqual({ kind: 'ready', version: ready(ASKED) });

    pinned.set(OTHER);
    const seen: string[] = [kindOf(watch.phase())];
    await settle();
    seen.push(kindOf(watch.phase()));

    expect(seen).not.toContain('substituted');
    expect(watch.phase()).toEqual({ kind: 'ready', version: ready(OTHER) });
    expect(read.mock.calls.map(([id]) => id)).toEqual([ASKED, OTHER]);
  });

  it.each([
    ['names no version', ''],
    ['is not an id', '../accounts'],
    ['is another kind of id', 'ogs_aaaa'],
  ])('refuses an address that %s without reading anything, and stays refused', async (_case, value) => {
    const read = vi.fn();
    const { watch } = watching(read, value);
    await settle();

    expect(watch.phase()).toEqual({ kind: 'invalid' });
    await pollOnce();
    watch.recheck();
    await settle();
    expect(kindOf(watch.phase())).toBe('invalid');
    expect(read).not.toHaveBeenCalled();
  });

  it('answers only its own action, and does not start a second read while one is under way', async () => {
    let finish: (version: Version) => void = () => undefined;
    const read = vi.fn(() => new Promise<Version>((resolve) => { finish = resolve; }));
    const { watch } = watching(read);
    TestBed.tick();

    watch.onAction('versions');
    watch.onAction(PINNED_VERSION_RECHECK_ACTION.id);
    watch.recheck();
    expect(read).toHaveBeenCalledTimes(1);

    finish(ready());
    await settle();
    expect(kindOf(watch.phase())).toBe('ready');
    watch.onAction('versions');
    await settle();
    expect(read).toHaveBeenCalledTimes(1);
  });

  it('stops asking when the page that held it is gone', async () => {
    const read = vi.fn(async () => ready());
    watching(read);
    await settle();
    expect(vi.getTimerCount()).toBe(1);

    TestBed.resetTestingModule();

    expect(vi.getTimerCount()).toBe(0);
    vi.advanceTimersByTime(PINNED_VERSION_RECHECK_MS * 2);
    expect(read).toHaveBeenCalledTimes(1);
  });
});
