import type { Provider } from '@angular/core';
import type { ComponentFixture } from '@angular/core/testing';
import { By } from '@angular/platform-browser';

import type { TickerOption } from '../../ticker-range-picker/ticker-range-picker.types';
import {
  fakeEnsureCoverage,
  fakeVendorCatalog,
  provideFakeEnsureCoverage,
  provideFakeVendorCatalog,
  type FakeEnsureCoverage,
  type FakeVendorCatalog,
} from '../../symbol-catalog/testing/fake-symbol-catalog';
import type { VendorSymbolEntry } from '../../symbol-catalog/vendor-catalog.service';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../../ticker-catalog/testing/fake-ticker-catalog';
import { SymbolPickerComponent } from '../symbol-picker.component';

/**
 * The test world a host spec needs to render anything that embeds the
 * picker family: a fake lake catalog, a fake vendor catalog and a fake
 * coverage gate, so no spec mounts the real HTTP/gate chain by accident.
 *
 * `provideFleetDirectory` is the precedent: one composition point per
 * cross-cutting fake, instead of every spec re-stating the trio.
 */

/** A standard held pool: two runnable lake rows most pickers can offer. */
export const STANDARD_PICKER_POOL: readonly TickerOption[] = [
  {
    symbol: 'SPY',
    name: 'SPDR S&P 500',
    firstHeld: '2024-01-02',
    lastHeld: '2026-09-18',
  },
  {
    symbol: 'AAPL',
    name: 'Apple Inc.',
    firstHeld: '2024-01-02',
    lastHeld: '2026-09-18',
  },
];

export interface FakePickerWorld {
  /** Spread into `providers`. */
  readonly providers: Provider[];
  /** The coverage gate fake — assert `ensureCalls`/`refusals` through it. */
  readonly coverage: FakeEnsureCoverage;
  readonly vendor: FakeVendorCatalog;
}

export function fakePickerWorld(
  pool: readonly TickerOption[] = STANDARD_PICKER_POOL,
  vendorEntries: readonly VendorSymbolEntry[] = [],
): FakePickerWorld {
  const coverage = fakeEnsureCoverage();
  const vendor = fakeVendorCatalog(vendorEntries);
  return {
    coverage,
    vendor,
    providers: [
      provideFakeTickerCatalog(fakeTickerCatalog(pool)),
      provideFakeVendorCatalog(vendor),
      provideFakeEnsureCoverage(coverage),
    ],
  };
}

/** Open the single card's dropdown — the combobox click an operator makes. */
export function openDropdown<T>(fixture: ComponentFixture<T>): void {
  const box = fixture.nativeElement.querySelector('[role="combobox"]');
  if (box === null) throw new Error('No picker combobox is rendered.');
  box.click();
  fixture.detectChanges();
}

/**
 * Flush the gate's promise chain — the fake ensure resolves immediately, so
 * two microtasks carry the verdict to the card's commit.
 */
export async function flushGate(fixture: ComponentFixture<unknown>): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
  fixture.detectChanges();
}

/** The first symbol picker under `fixture`, for reads and model-driven picks. */
export function symbolPicker<T>(fixture: ComponentFixture<T>): SymbolPickerComponent {
  const de = fixture.debugElement.query(By.directive(SymbolPickerComponent));
  if (de === null) throw new Error('No app-symbol-picker is rendered.');
  return de.componentInstance as SymbolPickerComponent;
}

/**
 * A trader symbol edit as the specs make one: set the picker's model and
 * let its output drive the host, exactly as a real pick does.
 */
export function pickSymbol<T>(
  fixture: ComponentFixture<T>,
  value: string,
): SymbolPickerComponent {
  const picker = symbolPicker(fixture);
  picker.symbol.set(value);
  fixture.detectChanges();
  return picker;
}
