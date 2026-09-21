import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { MultiTickerRangePickerComponent } from './multi-ticker-range-picker.component';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
  type FakeTickerCatalog,
} from '../ticker-catalog/testing/fake-ticker-catalog';
import {
  fakeEnsureCoverage,
  fakeVendorCatalog,
  provideFakeEnsureCoverage,
  provideFakeVendorCatalog,
  type FakeEnsureCoverage,
  type FakeVendorCatalog,
} from '../symbol-catalog/testing/fake-symbol-catalog';
import type { VendorSymbolEntry } from '../symbol-catalog/vendor-catalog.service';
import type { MultiTickerRange } from './multi-ticker-range-picker.types';

describe('MultiTickerRangePickerComponent', () => {
  const baseValue: MultiTickerRange = {
    symbols: ['SPY'],
    from: '2025-04-01',
    to: '2025-04-30',
    resolution: 'minute',
  };
  const pool: TickerOption[] = [
    { symbol: 'SPY', name: 'SPDR S&P 500', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
    { symbol: 'QQQ', name: 'Invesco QQQ', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
  ];
  const listedUnheld: VendorSymbolEntry = {
    symbol: 'TSLA',
    name: 'Tesla, Inc.',
    asset_class: 'us_equity',
    exchange: 'NASDAQ',
    status: 'active',
  };

  let fixture: ComponentFixture<MultiTickerRangePickerComponent>;
  let catalog: FakeTickerCatalog;
  let vendor: FakeVendorCatalog;
  let coverage: FakeEnsureCoverage;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    catalog = fakeTickerCatalog(pool);
    vendor = fakeVendorCatalog([listedUnheld]);
    coverage = fakeEnsureCoverage();
    await TestBed.configureTestingModule({
      providers: [
        provideFakeTickerCatalog(catalog),
        provideFakeVendorCatalog(vendor),
        provideFakeEnsureCoverage(coverage),
      ],
      imports: [MultiTickerRangePickerComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(MultiTickerRangePickerComponent);
    fixture.componentRef.setInput('value', baseValue);
  });

  it('composes the three sub-components', () => {
    fixture.detectChanges();
    expect(
      fixture.nativeElement.querySelector('app-multi-instrument-card'),
    ).not.toBeNull();
    expect(
      fixture.nativeElement.querySelector('app-time-window-card'),
    ).not.toBeNull();
    expect(
      fixture.nativeElement.querySelector('app-sampling-card'),
    ).not.toBeNull();
  });

  it('hideSampling=true collapses the Sampling card', () => {
    fixture.componentRef.setInput('hideSampling', true);
    fixture.detectChanges();
    expect(
      fixture.nativeElement.querySelector('app-sampling-card'),
    ).toBeNull();
  });

  it('passes availableMultipliers through to SamplingCard', () => {
    fixture.componentRef.setInput('availableMultipliers', [1, 5, 15]);
    fixture.detectChanges();
    expect(
      fixture.nativeElement.querySelector('.multiplier__select'),
    ).not.toBeNull();
  });

  it('renders the universe count in the summary line', () => {
    fixture.componentRef.setInput('value', {
      ...baseValue,
      symbols: ['SPY', 'QQQ', 'IWM'],
    });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('3 tickers');
  });

  it('offers the joined universe — a listed symbol the lake does not hold is on the menu', () => {
    fixture.detectChanges();

    const input = fixture.nativeElement.querySelector(
      '[aria-label="Search to add a ticker"]',
    ) as HTMLInputElement;
    input.value = 'TSLA';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();

    const rows = Array.from(fixture.nativeElement.querySelectorAll('[role="option"]'));
    expect(rows.some((row) => (row as HTMLElement).textContent?.includes('TSLA'))).toBe(true);
  });

  it('gates an unheld add on its backfill — the symbol joins the universe only once covered', async () => {
    fixture.detectChanges();

    const input = fixture.nativeElement.querySelector(
      '[aria-label="Search to add a ticker"]',
    ) as HTMLInputElement;
    input.value = 'TSLA';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    const option = Array.from(fixture.nativeElement.querySelectorAll('[role="option"]')).find(
      (row) => (row as HTMLElement).textContent?.includes('TSLA'),
    ) as HTMLElement;
    option.click();
    fixture.detectChanges();

    // The gate runs on the tree the batch run reads — the default is the
    // split-adjusted tree the cross-sectional worker requests.
    expect(coverage.ensureCalls).toEqual([
      { symbol: 'TSLA', mode: 'polygon_split_adjusted' },
    ]);
    expect(fixture.componentInstance.value().symbols).toEqual(['SPY']);

    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();

    expect(fixture.componentInstance.value().symbols).toEqual(['SPY', 'TSLA']);
  });

  it('a held add needs no gate', () => {
    fixture.detectChanges();

    const input = fixture.nativeElement.querySelector(
      '[aria-label="Search to add a ticker"]',
    ) as HTMLInputElement;
    input.value = 'QQQ';
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    const option = Array.from(fixture.nativeElement.querySelectorAll('[role="option"]')).find(
      (row) => (row as HTMLElement).textContent?.includes('QQQ'),
    ) as HTMLElement;
    option.click();
    fixture.detectChanges();

    expect(coverage.ensureCalls).toEqual([]);
    expect(fixture.componentInstance.value().symbols).toEqual(['SPY', 'QQQ']);
  });
});
