import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';

import { SymbolPickerComponent } from './symbol-picker.component';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import {
  fakeEnsureCoverage,
  fakeVendorCatalog,
  provideFakeEnsureCoverage,
  provideFakeVendorCatalog,
  type FakeEnsureCoverage,
  type FakeVendorCatalog,
} from '../symbol-catalog/testing/fake-symbol-catalog';
import type { VendorSymbolEntry } from '../symbol-catalog/vendor-catalog.service';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
  type FakeTickerCatalog,
} from '../ticker-catalog/testing/fake-ticker-catalog';

/**
 * The symbol-only wrapper most hosts bind (`[(symbol)]`). Its own contract
 * is the projection: the card's joined universe, gate and degraded banner
 * must arrive at the host as a bare string — the window half of the card's
 * model never crosses back. The card's own behaviors are pinned by
 * `instrument-card.component.spec.ts`; these tests pin the wrapper seam.
 */
describe('SymbolPickerComponent', () => {
  const pool: TickerOption[] = [
    {
      symbol: 'SPY',
      name: 'SPDR S&P 500 ETF',
      exchange: 'ARCA',
      firstHeld: '2024-05-20',
      lastHeld: '2025-04-30',
    },
  ];
  const listedUnheld: VendorSymbolEntry = {
    symbol: 'TSLA',
    name: 'Tesla, Inc.',
    asset_class: 'us_equity',
    exchange: 'NASDAQ',
    status: 'active',
  };

  let fixture: ComponentFixture<SymbolPickerComponent>;
  let component: SymbolPickerComponent;
  let catalog: FakeTickerCatalog;
  let vendor: FakeVendorCatalog;
  let coverage: FakeEnsureCoverage;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    catalog = fakeTickerCatalog(pool);
    vendor = fakeVendorCatalog([listedUnheld]);
    coverage = fakeEnsureCoverage();
    await TestBed.configureTestingModule({
      imports: [SymbolPickerComponent],
      providers: [
        provideFakeTickerCatalog(catalog),
        provideFakeVendorCatalog(vendor),
        provideFakeEnsureCoverage(coverage),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(SymbolPickerComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('symbol', 'SPY');
  });

  function openDropdown(): void {
    const tickerBox = fixture.nativeElement.querySelector('[role="combobox"]') as HTMLElement;
    expect(tickerBox).not.toBeNull();
    tickerBox.click();
    fixture.detectChanges();
  }

  /** Flushes the gate's promise chain; the fake ensure resolves immediately. */
  async function flushGate(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();
  }

  it('renders the bound symbol', () => {
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('SPY');
  });

  it('offers the joined universe — a listed-but-unheld symbol is on the menu', () => {
    fixture.detectChanges();
    openDropdown();
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('TSLA');
    expect(text).toContain('not held');
  });

  it('emits exactly the picked symbol — the window half of the card model never crosses back', async () => {
    fixture.detectChanges();
    openDropdown();

    const option = Array.from(fixture.nativeElement.querySelectorAll('[role="option"]')).find(
      (candidate) => (candidate as HTMLElement).textContent?.includes('SPY'),
    ) as HTMLElement;
    option.click();
    fixture.detectChanges();

    expect(component.symbol()).toBe('SPY');
  });

  it('gates an unheld pick — the symbol arrives only once the lake confirms coverage', async () => {
    fixture.detectChanges();
    openDropdown();

    const option = Array.from(fixture.nativeElement.querySelectorAll('[role="option"]')).find(
      (candidate) => (candidate as HTMLElement).textContent?.includes('TSLA'),
    ) as HTMLElement;
    option.click();
    fixture.detectChanges();

    expect(coverage.ensureCalls).toContainEqual({
      symbol: 'TSLA',
      mode: 'polygon_split_adjusted',
    });
    expect(component.symbol()).toBe('SPY');

    await flushGate();
    expect(component.symbol()).toBe('TSLA');
  });

  it('degrades visibly when the vendor catalog is dark', () => {
    vendor.entries.set(null);
    vendor.unavailable.set('catalog endpoint down');
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Live catalog unavailable');
    expect(text).toContain('showing lake holdings');
  });
});
