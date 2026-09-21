import { signal, type Provider, type WritableSignal } from '@angular/core';

import {
  VendorCatalogService,
  type VendorSymbolEntry,
} from '../vendor-catalog.service';
import type {
  BackfillableMode,
  CoverageGateSession,
  CoverageGateState,
} from '../ensure-coverage.service';
import { EnsureCoverageService } from '../ensure-coverage.service';
import type { PriceAdjustmentMode } from '../../data-lake';

/**
 * One fake per injected collaborator, each starting settled — the same
 * discipline as `ticker-catalog/testing`: typed against the members the real
 * service exposes, because a `useValue` provider is never checked against the
 * class it replaces.
 */
export interface FakeVendorCatalog {
  readonly entries: WritableSignal<readonly VendorSymbolEntry[] | null>;
  readonly loading: WritableSignal<boolean>;
  readonly unavailable: WritableSignal<string | null>;
  reloadCount: number;
  reload(): void;
}

export function fakeVendorCatalog(
  entries: readonly VendorSymbolEntry[] = [],
): FakeVendorCatalog {
  return {
    entries: signal(entries),
    loading: signal(false),
    unavailable: signal(null),
    reloadCount: 0,
    reload(): void {
      this.reloadCount++;
    },
  };
}

export function provideFakeVendorCatalog(catalog: FakeVendorCatalog): Provider {
  return { provide: VendorCatalogService, useValue: catalog };
}

/** A gate session a card can hold: the strip plus a resolvable verdict. */
export interface FakeCoverageSession extends CoverageGateSession {
  readonly state: WritableSignal<CoverageGateState | null>;
  cancelCalls: number;
  resolve(ready: boolean): void;
}

function gateState(
  symbol: string,
  overrides: Partial<CoverageGateState>,
): CoverageGateState {
  return {
    symbol,
    mode: 'raw',
    phase: 'backfilling',
    percent: null,
    reason: null,
    message: null,
    ...overrides,
  };
}

function fakeSession(
  symbol: string,
  state: CoverageGateState | null,
  ready?: boolean,
): FakeCoverageSession {
  let resolveDone!: (ready: boolean) => void;
  const done = new Promise<boolean>((resolve) => (resolveDone = resolve));
  if (ready !== undefined) resolveDone(ready);
  return {
    state: signal(state),
    done,
    cancelCalls: 0,
    cancel(): Promise<void> {
      this.cancelCalls++;
      this.state.set(null);
      return Promise.resolve();
    },
    resolve(ready: boolean): void {
      resolveDone(ready);
    },
  };
}

/** A held-open gate the test settles with `session.resolve(ready)`. */
export function fakeHeldSession(symbol: string): FakeCoverageSession {
  return fakeSession(symbol, gateState(symbol, {}));
}

/** A gate that has already failed — the strip the card should render. */
export function fakeFailedSession(
  symbol: string,
  reason: string,
  message: string,
): FakeCoverageSession {
  return fakeSession(
    symbol,
    gateState(symbol, { phase: 'failed', reason, message }),
    false,
  );
}

export interface FakeEnsureCoverage {
  readonly ensureCalls: { symbol: string; mode: BackfillableMode }[];
  readonly refusals: {
    symbol: string;
    mode: PriceAdjustmentMode;
    reason: string;
    message: string;
  }[];
  /**
   * Verdicts for immediate (non-held) ensures, popped in order; an empty
   * script answers `true`. A `false` hands the card a failed strip, as the
   * real service does.
   */
  readonly ensureResults: boolean[];
  /** When set, the next ensure() returns a session the test settles. */
  holdNext: boolean;
  readonly held: FakeCoverageSession[];
  ensure(symbol: string, mode: BackfillableMode): CoverageGateSession;
  refuse(
    symbol: string,
    mode: PriceAdjustmentMode,
    reason: string,
    message: string,
  ): CoverageGateSession;
}

export function fakeEnsureCoverage(): FakeEnsureCoverage {
  return {
    ensureCalls: [],
    refusals: [],
    ensureResults: [],
    holdNext: false,
    held: [],
    ensure(symbol: string, mode: BackfillableMode): CoverageGateSession {
      this.ensureCalls.push({ symbol, mode });
      if (this.holdNext) {
        this.holdNext = false;
        const session = fakeHeldSession(symbol);
        this.held.push(session);
        return session;
      }
      const ready = this.ensureResults.shift() ?? true;
      return ready
        ? fakeSession(symbol, null, true)
        : fakeFailedSession(symbol, 'backfill_failed', 'The backfill job failed.');
    },
    refuse(
      symbol: string,
      mode: PriceAdjustmentMode,
      reason: string,
      message: string,
    ): CoverageGateSession {
      this.refusals.push({ symbol, mode, reason, message });
      // The real service renders a refusal through the same failed state as
      // a job failure — mirror that so strip-visibility tests are honest.
      return fakeFailedSession(symbol, reason, message);
    },
  };
}

export function provideFakeEnsureCoverage(fake: FakeEnsureCoverage): Provider {
  return { provide: EnsureCoverageService, useValue: fake };
}
