import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { describe, it, expect, beforeEach } from 'vitest';
import { InstrumentCardComponent } from './instrument-card.component';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
  type FakeTickerCatalog,
} from '../../ticker-catalog/testing/fake-ticker-catalog';
import type {
  TickerOption,
  TickerRange,
} from '../ticker-range-picker.types';

describe('InstrumentCardComponent', () => {
  const baseValue: TickerRange = {
    symbol: 'SPY',
    from: '2025-04-01',
    to: '2025-04-30',
    resolution: 'minute',
  };
  const pool: TickerOption[] = [
    {
      symbol: 'SPY',
      name: 'SPDR S&P 500 ETF',
      exchange: 'ARCA',
      first: '2024-05-20',
      last: '2025-04-30',
    },
    {
      symbol: 'QQQ',
      name: 'Invesco QQQ',
      exchange: 'NASDAQ',
      first: '2024-05-20',
      last: '2025-04-30',
    },
  ];

  let fixture: ComponentFixture<InstrumentCardComponent>;
  let component: InstrumentCardComponent;
  let catalog: FakeTickerCatalog;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    catalog = fakeTickerCatalog(pool);
    await TestBed.configureTestingModule({
      imports: [InstrumentCardComponent],
      providers: [provideRouter([]), provideFakeTickerCatalog(catalog)],
    }).compileComponents();

    fixture = TestBed.createComponent(InstrumentCardComponent);
    component = fixture.componentInstance;

    fixture.componentRef.setInput('value', baseValue);
  });

  function openDropdown(): void {
    const tickerBox: HTMLElement | null =
      fixture.nativeElement.querySelector('[role="combobox"]');
    expect(tickerBox).not.toBeNull();
    tickerBox?.click();
    fixture.detectChanges();
  }

  it('renders the current symbol and exchange', () => {
    fixture.detectChanges();
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('SPY');
    expect(text).toContain('ARCA');
  });

  it('opens the dropdown on click and shows the recent list when query is empty', () => {
    catalog.recent.set(['QQQ']);
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Recent');
    expect(text).toContain('Invesco QQQ');
  });

  it('updates value().symbol when a ticker is picked', () => {
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker(pool[1]);
    fixture.detectChanges();

    expect(component.value().symbol).toBe('QQQ');
  });

  // The catalog is what the lake holds, so every instrument it lists is
  // selectable — the regression this replaces was a hardcoded eleven-symbol
  // pool that hid GLD, DIA, SLV, GE and STRL from every UI path even though
  // the lake had them fully backfilled.
  it('offers whatever the catalog lists, not a fixed roster', () => {
    catalog.pool.set([
      { symbol: 'GLD', name: 'SPDR Gold Shares', exchange: 'ARCA', last: '2026-09-04' },
    ]);
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('GLD');
    expect(text).toContain('SPDR Gold Shares');
  });

  it('reports the coverage span of the selected instrument', () => {
    fixture.detectChanges();
    expect(component.selectedTickerFirst()).toBe('2024-05-20');
    expect(component.selectedTickerLast()).toBe('2025-04-30');
    expect(fixture.nativeElement.textContent).toContain('lake coverage');
  });

  it('says why the list is empty when the lake did not answer, and can retry', () => {
    catalog.pool.set([]);
    catalog.unavailable.set('The data lake is unreachable.');
    fixture.detectChanges();
    openDropdown();

    expect(fixture.nativeElement.textContent).toContain('The data lake is unreachable.');

    const retry: HTMLButtonElement | null =
      fixture.nativeElement.querySelector('.dropdown__retry');
    expect(retry).not.toBeNull();
    retry?.click();
    expect(catalog.reloadCount).toBe(1);
  });

  it('points a no-match search at backfilling rather than a page with no add flow', () => {
    fixture.detectChanges();
    openDropdown();
    component.onSearchInput('NOPE');
    fixture.detectChanges();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('holds no bars');
    const link: HTMLAnchorElement | null =
      fixture.nativeElement.querySelector('.dropdown__empty a');
    expect(link?.getAttribute('href')).toBe('/data-lake');
  });

  // Sidecar validator rejects weekend endpoints; pickTicker derives
  // ``from = last - 30 days`` which lands on a weekend whenever
  // ``last`` is Mon-Wed. Guard both endpoints.
  it('pickTicker bumps a weekend-derived from date back to Friday', () => {
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    // last = Mon 2026-05-25 → last-30 = Sat 2026-04-25 → walks to
    // Fri 2026-04-24. ``to`` is the supplied weekday Mon (no walk).
    component.pickTicker({
      symbol: 'AAPL',
      name: 'Apple',
      exchange: 'NASDAQ',
      first: '2020-01-02',
      last: '2026-05-25',
    });
    fixture.detectChanges();

    expect(component.value().from).toBe('2026-04-24');
    expect(component.value().to).toBe('2026-05-25');
  });

  it('pickTicker clamps the proposed window to where coverage starts', () => {
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    // The lake holds three days of STRL. A blind ``last - 30`` would open on
    // 2026-04-25, four weeks of which has no bars to read.
    component.pickTicker({
      symbol: 'STRL',
      name: 'Sterling Infrastructure, Inc.',
      exchange: 'NASDAQ',
      first: '2026-05-21',
      last: '2026-05-25',
    });
    fixture.detectChanges();

    expect(component.value().from).toBe('2026-05-21');
    expect(component.value().to).toBe('2026-05-25');
  });
});
