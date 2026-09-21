import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { describe, it, expect, beforeEach } from 'vitest';
import { InstrumentCardComponent } from './instrument-card.component';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
  type FakeTickerCatalog,
} from '../../ticker-catalog/testing/fake-ticker-catalog';
import {
  fakeAlpacaAssetCatalog,
  fakeEnsureCoverage,
  provideFakeAlpacaAssetCatalog,
  provideFakeEnsureCoverage,
  type FakeAlpacaAssetCatalog,
  type FakeEnsureCoverage,
} from '../../symbol-catalog/testing/fake-symbol-catalog';
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
  let alpaca: FakeAlpacaAssetCatalog;
  let coverage: FakeEnsureCoverage;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    catalog = fakeTickerCatalog(pool);
    alpaca = fakeAlpacaAssetCatalog();
    coverage = fakeEnsureCoverage();
    await TestBed.configureTestingModule({
      imports: [InstrumentCardComponent],
      providers: [
        provideRouter([]),
        provideFakeTickerCatalog(catalog),
        provideFakeAlpacaAssetCatalog(alpaca),
        provideFakeEnsureCoverage(coverage),
      ],
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

  /** Flushes the gate's promise chain; the fake ensure resolves immediately. */
  async function flushGate(): Promise<void> {
    await Promise.resolve();
    await Promise.resolve();
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

  // The joined universe: the vendor's listings are offerable even when the
  // lake has never held them — that is what makes any symbol reachable.
  it('appends listed-but-unheld symbols after the lake holdings', () => {
    alpaca.entries.set([
      { symbol: 'TSLA', name: 'Tesla, Inc.', asset_class: 'us_equity', exchange: 'NASDAQ', status: 'active', tradable: true },
    ]);
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('TSLA');
    // An unheld row says so instead of a fake held date.
    expect(text).toContain('not held');
  });

  it('renders each row through the shared asset identity', () => {
    fixture.detectChanges();
    openDropdown();

    const rows = fixture.nativeElement.querySelectorAll('.dropdown__scroll .row');
    expect(rows.length).toBeGreaterThan(0);
    const identities = fixture.nativeElement.querySelectorAll(
      '.dropdown__scroll .row app-asset-identity',
    );
    expect(identities.length).toBe(rows.length);
  });

  it('never offers a delisted symbol the lake does not already hold', () => {
    alpaca.entries.set([
      { symbol: 'AAPL', name: 'Apple Inc.', asset_class: 'us_equity', exchange: 'NASDAQ', status: 'active', tradable: true },
      { symbol: 'OLD', name: 'Delisted Corp', asset_class: 'us_equity', exchange: 'NYSE', status: 'inactive', tradable: false },
    ]);
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('AAPL');
    expect(text).not.toContain('Delisted Corp');
  });

  it('badges a held symbol the vendor has delisted, and keeps it pickable', () => {
    catalog.view.pool.set([
      { symbol: 'OLD', name: 'Delisted Corp', exchange: 'NYSE', firstHeld: '2019-01-02', lastHeld: '2020-01-31' },
    ]);
    alpaca.entries.set([
      { symbol: 'OLD', name: 'Delisted Corp', asset_class: 'us_equity', exchange: 'NYSE', status: 'inactive', tradable: false },
    ]);
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('OLD');
    expect(text).toContain('delisted');
  });

  it('shows a banner — not a blank list — when the live catalog is dark', () => {
    alpaca.unavailable.set('The broker catalog is unreachable.');
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Live catalog unavailable');
    // Degraded, not empty: the lake's own answer still stands.
    expect(text).toContain('SPY');
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

  it('says a no-match search found no listed symbol', () => {
    fixture.detectChanges();
    openDropdown();
    component.onSearchInput('NOPE');
    fixture.detectChanges();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('No listed symbol matches that');
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

  // An unheld pick is no longer a dead end: the gate backfills it, and only
  // a covered symbol reaches value(). This is the populate-then-use loop.
  it('gates an unheld pick on its backfill and selects it once covered', async () => {
    coverage.ensureResults.push(true);
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker({ symbol: 'NVDA', name: 'NVIDIA', exchange: 'NASDAQ' });
    await flushGate();

    expect(coverage.ensureCalls).toEqual([
      { symbol: 'NVDA', mode: 'polygon_split_adjusted' },
    ]);
    expect(component.value().symbol).toBe('NVDA');
  });

  it('leaves the selection untouched when the backfill fails, showing the strip', async () => {
    coverage.ensureResults.push(false);
    coverage.active.set({
      symbol: 'NVDA',
      phase: 'failed',
      percent: null,
      reason: 'backfill_failed',
      message: 'The vendor refused the range.',
    });
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker({ symbol: 'NVDA', name: 'NVIDIA', exchange: 'NASDAQ' });
    await flushGate();

    expect(component.value().symbol).toBe('SPY');
    const strip: HTMLElement | null =
      fixture.nativeElement.querySelector('.dropdown__gate');
    expect(strip).not.toBeNull();
    expect(strip?.textContent).toContain('The vendor refused the range.');
    // The failure's reason code renders through the receipt-label pipe.
    expect(strip?.querySelector('.dropdown__gate-msg .mono')).not.toBeNull();
    // Cancel is offered so the operator can walk away cleanly.
    expect(strip?.textContent).toContain('Retry');
    expect(strip?.textContent).toContain('Dismiss');
  });

  it('refuses the gate loudly for a view no backfill can produce', () => {
    fixture.componentRef.setInput('adjustmentMode', 'lean_adjusted');
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker({ symbol: 'NVDA', name: 'NVIDIA', exchange: 'NASDAQ' });
    fixture.detectChanges();

    expect(coverage.ensureCalls).toEqual([]);
    expect(coverage.refusals).toEqual([
      {
        symbol: 'NVDA',
        reason: 'view_not_backfillable',
        // The refusal names the view the way the strip renders it.
        message: expect.stringContaining('Lean Adjusted'),
      },
    ]);
    expect(component.value().symbol).toBe('SPY');
  });

  it('dismisses the gate strip without selecting', async () => {
    coverage.ensureResults.push(false);
    coverage.active.set({
      symbol: 'NVDA',
      phase: 'failed',
      percent: null,
      reason: 'backfill_failed',
      message: 'The vendor refused the range.',
    });
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();
    component.pickTicker({ symbol: 'NVDA', name: 'NVIDIA', exchange: 'NASDAQ' });
    await flushGate();

    const buttons = fixture.nativeElement.querySelectorAll('.dropdown__gate button');
    const dismiss = Array.from(buttons).find(
      (b) => (b as HTMLButtonElement).textContent?.trim() === 'Dismiss',
    ) as HTMLButtonElement | undefined;
    dismiss?.click();
    await flushGate();

    expect(coverage.cancelCount).toBe(1);
    expect(
      fixture.nativeElement.querySelector('.dropdown__gate'),
    ).toBeNull();
  });

  it('distinguishes an empty lake from a search that matched nothing', () => {
    catalog.view.pool.set([]);
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('the lake holds nothing yet');
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

  // A host may supply its own universe when it owns membership outright —
  // the backfill panel's vendor list, say. The card renders it verbatim,
  // with no gate: the host decides what its symbols mean.
  it('offers a host-supplied universe instead of the joined catalog when given one', () => {
    alpaca.entries.set([
      { symbol: 'TSLA', name: 'Tesla, Inc.', asset_class: 'us_equity', exchange: 'NASDAQ', status: 'active', tradable: true },
    ]);
    fixture.componentRef.setInput('universe', [
      { symbol: 'QQQ', name: 'Invesco QQQ Trust', exchange: 'NASDAQ' },
    ]);
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('QQQ');
    expect(text).not.toContain('TSLA');
    // No gate on a host universe: the pick applies directly.
    component.pickTicker({ symbol: 'QQQ', name: 'Invesco QQQ Trust' });
    fixture.detectChanges();
    expect(coverage.ensureCalls).toEqual([]);
    expect(component.value().symbol).toBe('QQQ');
  });

  it('offers the tree its host names', () => {
    // Every mode of the fake starts from the same seed, so a symbol only the
    // raw tree holds is what proves the picker read that tree, not the default.
    catalog.viewFor('raw').pool.set([
      { symbol: 'RAWONLY', name: 'Raw-tree only', exchange: 'ARCA', lastHeld: '2026-09-04' },
    ]);
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.detectChanges();
    openDropdown();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('RAWONLY');
    expect(text).not.toContain('QQQ');
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
    expect(alpaca.reloadCount).toBe(before + 1);
  });

  it('does not re-read the lake for a host-supplied universe', () => {
    fixture.componentRef.setInput('universe', [
      { symbol: 'QQQ', name: 'Invesco QQQ Trust', exchange: 'NASDAQ' },
    ]);
    fixture.detectChanges();
    const before = catalog.view.reloadCount;
    openDropdown();

    expect(catalog.view.reloadCount).toBe(before);
    expect(alpaca.reloadCount).toBe(before);
  });
});
