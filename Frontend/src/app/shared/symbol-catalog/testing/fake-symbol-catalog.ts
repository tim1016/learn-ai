import { signal, type Provider, type WritableSignal } from '@angular/core';

import {
  AlpacaAssetCatalogService,
  type AlpacaSymbolEntry,
} from '../alpaca-asset-catalog.service';
import type {
  BackfillableMode,
  CoverageGateState,
} from '../ensure-coverage.service';
import { EnsureCoverageService } from '../ensure-coverage.service';

/**
 * One fake per injected collaborator, each starting settled — the same
 * discipline as `ticker-catalog/testing`: typed against the members the real
 * service exposes, because a `useValue` provider is never checked against the
 * class it replaces.
 */
export interface FakeAlpacaAssetCatalog {
  readonly entries: WritableSignal<readonly AlpacaSymbolEntry[] | null>;
  readonly loading: WritableSignal<boolean>;
  readonly unavailable: WritableSignal<string | null>;
  reloadCount: number;
  reload(): void;
}

export function fakeAlpacaAssetCatalog(
  entries: readonly AlpacaSymbolEntry[] = [],
): FakeAlpacaAssetCatalog {
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

export function provideFakeAlpacaAssetCatalog(catalog: FakeAlpacaAssetCatalog): Provider {
  return { provide: AlpacaAssetCatalogService, useValue: catalog };
}

export interface FakeEnsureCoverage {
  readonly active: WritableSignal<CoverageGateState | null>;
  /** Scripted answers, popped in order; an empty script answers `true`. */
  readonly ensureResults: boolean[];
  readonly ensureCalls: { symbol: string; mode: BackfillableMode }[];
  readonly refusals: { symbol: string; reason: string; message: string }[];
  cancelCount: number;
  lastRetryMode: BackfillableMode | null;
  ensure(symbol: string, mode: BackfillableMode): Promise<boolean>;
  refuse(symbol: string, reason: string, message: string): void;
  cancel(): Promise<void>;
  retry(mode: BackfillableMode): Promise<boolean>;
}

export function fakeEnsureCoverage(): FakeEnsureCoverage {
  return {
    active: signal(null),
    ensureResults: [],
    ensureCalls: [],
    refusals: [],
    cancelCount: 0,
    lastRetryMode: null,
    ensure(symbol: string, mode: BackfillableMode): Promise<boolean> {
      this.ensureCalls.push({ symbol, mode });
      return Promise.resolve(this.ensureResults.shift() ?? true);
    },
    refuse(symbol: string, reason: string, message: string): void {
      this.refusals.push({ symbol, reason, message });
    },
    cancel(): Promise<void> {
      this.cancelCount++;
      return Promise.resolve();
    },
    retry(mode: BackfillableMode): Promise<boolean> {
      this.lastRetryMode = mode;
      return Promise.resolve(this.ensureResults.shift() ?? true);
    },
  };
}

export function provideFakeEnsureCoverage(fake: FakeEnsureCoverage): Provider {
  return { provide: EnsureCoverageService, useValue: fake };
}
