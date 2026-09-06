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
      firstHeld: '2024-05-20',
      lastHeld: '2025-04-30',
    },
    {
      symbol: 'QQQ',
      name: 'Invesco QQQ',
      exchange: 'NASDAQ',
      firstHeld: '2024-05-20',
      lastHeld: '2025-04-30',
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
    catalog.view.recent.set(['QQQ']);
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
    catalog.view.pool.set([
      { symbol: 'GLD', name: 'SPDR Gold Shares', exchange: 'ARCA', lastHeld: '2026-09-04' },
    ]);
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('GLD');
    expect(text).toContain('SPDR Gold Shares');
  });

  it('reports the days held for the selected instrument', () => {
    fixture.detectChanges();
    expect(component.selectedFirstHeld()).toBe('2024-05-20');
    expect(component.selectedLastHeld()).toBe('2025-04-30');
    expect(fixture.nativeElement.textContent).toContain('days held');
  });

  it('says why the list is empty when the lake did not answer, and can retry', () => {
    catalog.view.pool.set([]);
    catalog.view.unavailable.set('The data lake is unreachable.');
    fixture.detectChanges();
    openDropdown();

    expect(fixture.nativeElement.textContent).toContain('The data lake is unreachable.');

    const retry: HTMLButtonElement | null =
      fixture.nativeElement.querySelector('.dropdown__retry');
    expect(retry).not.toBeNull();
    // Delta, not an absolute: opening the dropdown re-reads too.
    const before = catalog.view.reloadCount;
    retry?.click();
    expect(catalog.view.reloadCount).toBe(before + 1);
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

  // The window on screen is the operator's. Switching instrument to compare
  // the same months must not silently retarget it — this branch never ran
  // before the lake-backed catalog, because no entry in the constant it
  // replaced carried a date.
  it('pickTicker keeps a window the new symbol actually has days in', () => {
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker(pool[1]);
    fixture.detectChanges();

    expect(component.value().from).toBe('2025-04-01');
    expect(component.value().to).toBe('2025-04-30');
  });

  // Sidecar validator rejects weekend endpoints; pickTicker derives
  // ``from = lastHeld - 30 days`` which lands on a weekend whenever
  // ``lastHeld`` is Mon-Wed. Guard both endpoints.
  it('pickTicker bumps a weekend-derived from date back to Friday', () => {
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    // Held days start after the window on screen (2025-04), so the window is
    // retargeted: lastHeld = Mon 2026-05-25 → −30 = Sat 2026-04-25 → walks to
    // Fri 2026-04-24. ``to`` is the supplied weekday Mon (no walk).
    component.pickTicker({
      symbol: 'AAPL',
      name: 'Apple',
      exchange: 'NASDAQ',
      firstHeld: '2026-04-01',
      lastHeld: '2026-05-25',
    });
    fixture.detectChanges();

    expect(component.value().from).toBe('2026-04-24');
    expect(component.value().to).toBe('2026-05-25');
  });

  it('pickTicker clamps the proposed window to the first day held', () => {
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    // The lake holds three days of STRL. A blind ``lastHeld - 30`` would open
    // on 2026-04-25, four weeks of which has no bars to read.
    component.pickTicker({
      symbol: 'STRL',
      name: 'Sterling Infrastructure, Inc.',
      exchange: 'NASDAQ',
      firstHeld: '2026-05-21',
      lastHeld: '2026-05-25',
    });
    fixture.detectChanges();

    expect(component.value().from).toBe('2026-05-21');
    expect(component.value().to).toBe('2026-05-25');
  });

  it('distinguishes an empty lake from a search that matched nothing', () => {
    catalog.view.pool.set([]);
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('holds no instruments yet');
    expect(text).not.toContain('matching that');
  });

  it('shows the read in flight rather than a stale failure while retrying', () => {
    catalog.view.pool.set([]);
    catalog.view.unavailable.set('The data lake is unreachable.');
    fixture.detectChanges();
    openDropdown();

    // The real service clears `unavailable` for the duration of a reload, so
    // the operator sees the retry working instead of the message that
    // prompted it.
    catalog.view.unavailable.set(null);
    catalog.view.loading.set(true);
    fixture.detectChanges();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Loading instruments…');
    expect(text).not.toContain('unreachable');
  });

  it('keeps the retry control out of the listbox', () => {
    catalog.view.pool.set([]);
    catalog.view.unavailable.set('The data lake is unreachable.');
    fixture.detectChanges();
    openDropdown();

    const listbox: HTMLElement | null =
      fixture.nativeElement.querySelector('[role="listbox"]');
    expect(listbox).not.toBeNull();
    // A focusable control inside a listbox is not an option: it lands in the
    // tab order of a widget navigated by arrow keys.
    expect(listbox?.querySelector('button, a')).toBeNull();
  });

  // Ticker Explorer's "Fetch Chain" is a live Polygon lookup, not a backtest.
  // Pinning it to lake holdings silently removed symbols that endpoint still
  // serves, so a host may supply its own universe.
  it('offers a host-supplied universe instead of the lake when given one', () => {
    catalog.view.pool.set([
      { symbol: 'GLD', name: 'SPDR Gold Shares', exchange: 'ARCA', lastHeld: '2026-09-04' },
    ]);
    fixture.componentRef.setInput('universe', [
      { symbol: 'QQQ', name: 'Invesco QQQ Trust', exchange: 'NASDAQ' },
    ]);
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('QQQ');
    expect(text).not.toContain('GLD');
    // Lake copy must not appear over a list the lake did not supply.
    expect(text).not.toContain('In the data lake');
  });

  it('asks the catalog for the tree its host names', () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.detectChanges();

    expect(catalog.modesRequested).toContain('raw');
  });

  // A symbol backfilled after this tab loaded must be selectable without a
  // page reload; the root-scoped resource otherwise serves its first answer
  // forever.
  it('re-reads the catalog when the dropdown opens', () => {
    fixture.detectChanges();
    const before = catalog.view.reloadCount;
    openDropdown();

    expect(catalog.view.reloadCount).toBe(before + 1);
  });

  it('does not re-read the lake for a host-supplied universe', () => {
    fixture.componentRef.setInput('universe', [
      { symbol: 'QQQ', name: 'Invesco QQQ Trust', exchange: 'NASDAQ' },
    ]);
    fixture.detectChanges();
    const before = catalog.view.reloadCount;
    openDropdown();

    expect(catalog.view.reloadCount).toBe(before);
  });
});
