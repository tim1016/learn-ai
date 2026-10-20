import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { describe, it, expect, beforeEach } from 'vitest';
import {
  flushGate,
  openDropdown,
} from '../../symbol-picker/testing/fake-picker-world';
import { InstrumentCardComponent } from './instrument-card.component';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
  type FakeTickerCatalog,
} from '../../ticker-catalog/testing/fake-ticker-catalog';
import {
  fakeVendorCatalog,
  fakeEnsureCoverage,
  provideFakeVendorCatalog,
  provideFakeEnsureCoverage,
  type FakeVendorCatalog,
  type FakeEnsureCoverage,
  type FakeCoverageSession,
} from '../../symbol-catalog/testing/fake-symbol-catalog';
import type { TickerOption, TickerRange } from '../ticker-range-picker.types';
import type { VendorSymbolEntry } from '../../symbol-catalog/vendor-catalog.service';
import { TradingCalendarService } from '../../date-picker';
import { fakeSessions, fakeTradingCalendar, provideFakeDateWorld } from '../../date-picker/testing/fake-trading-calendar';

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
  let alpaca: FakeVendorCatalog;
  let coverage: FakeEnsureCoverage;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    catalog = fakeTickerCatalog(pool);
    alpaca = fakeVendorCatalog();
    coverage = fakeEnsureCoverage();
    await TestBed.configureTestingModule({
      imports: [InstrumentCardComponent],
      providers: [
        provideRouter([]),
        provideFakeTickerCatalog(catalog),
        provideFakeVendorCatalog(alpaca),
        provideFakeEnsureCoverage(coverage),
        // Labor Day 2026 is closed; the calendar the page's date picker has read.
        ...provideFakeDateWorld(fakeTradingCalendar(fakeSessions('2025-01-01', '2026-12-31', { '2026-09-07': 'Labor Day' }), '2026-12-01')),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(InstrumentCardComponent);
    component = fixture.componentInstance;

    fixture.componentRef.setInput('value', baseValue);
  });

  /** A listed active symbol the lake does not hold — the gate's subject. */
  function seedListedUnheld(): void {
    alpaca.entries.set([
      {
        symbol: 'TSLA',
        name: 'Tesla, Inc.',
        asset_class: 'us_equity',
        exchange: 'NASDAQ',
        status: 'active',
      },
    ]);
  }


  it('renders the current instrument: its symbol, exchange and days held', () => {
    fixture.detectChanges();
    const text: string = fixture.nativeElement.textContent ?? '';
    expect.soft(text, 'renders the current symbol').toContain('SPY');
    expect.soft(text, 'renders the current exchange').toContain('ARCA');
    expect.soft(component.selectedFirstHeld(), 'reports the first day held for the selected instrument').toBe('2024-05-20');
    expect.soft(component.selectedLastHeld(), 'reports the last day held for the selected instrument').toBe('2025-04-30');
    expect.soft(fixture.nativeElement.textContent, 'renders the days held for the selected instrument').toContain('days held');
  });

  it('opens the dropdown on click and shows the recent list when query is empty', () => {
    catalog.view.recent.set(['QQQ']);
    fixture.detectChanges();
    openDropdown(fixture);

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Recent');
    expect(text).toContain('Invesco QQQ');
  });

  it('pickTicker selects the picked symbol and keeps a window the new symbol actually has days in', () => {
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker(pool[1]);
    fixture.detectChanges();

    expect.soft(component.value().symbol, 'updates value().symbol when a ticker is picked').toBe('QQQ');
    // The window on screen is the operator's. Switching instrument to compare
    // the same months must not silently retarget it — this branch never ran
    // before the lake-backed catalog, because no entry in the constant it
    // replaced carried a date.
    expect.soft(component.value().from, 'pickTicker keeps the window start the new symbol actually has days in').toBe('2025-04-01');
    expect.soft(component.value().to, 'pickTicker keeps the window end the new symbol actually has days in').toBe('2025-04-30');
  });

  // The catalog is what the lake holds, so every instrument it lists is
  // selectable — the regression this replaces was a hardcoded eleven-symbol
  // pool that hid GLD, DIA, SLV, GE and STRL from every UI path even though
  // the lake had them fully backfilled.
  it('offers whatever the catalog lists, not a fixed roster', () => {
    catalog.view.pool.set([
      {
        symbol: 'GLD',
        name: 'SPDR Gold Shares',
        exchange: 'ARCA',
        lastHeld: '2026-09-04',
      },
    ]);
    fixture.detectChanges();
    openDropdown(fixture);

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('GLD');
    expect(text).toContain('SPDR Gold Shares');
  });

  // The joined universe: the vendor's listings are offerable even when the
  // lake has never held them — that is what makes any symbol reachable.
  it('appends listed-but-unheld symbols after the lake holdings', () => {
    alpaca.entries.set([
      {
        symbol: 'TSLA',
        name: 'Tesla, Inc.',
        asset_class: 'us_equity',
        exchange: 'NASDAQ',
        status: 'active',
      },
    ]);
    fixture.detectChanges();
    openDropdown(fixture);

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('TSLA');
    // An unheld row says so instead of a fake held date.
    expect(text).toContain('not held');
  });

  it('opens the dropdown with each row through the shared asset identity, re-reading only the lake coverage', () => {
    fixture.detectChanges();
    const before = catalog.view.reloadCount;
    openDropdown(fixture);

    const rows = fixture.nativeElement.querySelectorAll('.dropdown__scroll .row');
    expect.soft(rows.length, 'renders at least one row').toBeGreaterThan(0);
    const identities = fixture.nativeElement.querySelectorAll(
      '.dropdown__scroll .row app-asset-identity',
    );
    expect.soft(identities.length, 'renders each row through the shared asset identity').toBe(rows.length);
    // A symbol backfilled after this tab loaded must be selectable without a
    // page reload; the root-scoped resource otherwise serves its first answer
    // forever. The vendor catalog is deliberately *not* re-read — one cached
    // read per tab, refreshed only through the degraded banner's Retry.
    expect.soft(catalog.view.reloadCount, 're-reads the lake coverage when the dropdown opens').toBe(before + 1);
    expect.soft(alpaca.reloadCount, 'does not re-read the vendor catalog when the dropdown opens').toBe(before);
  });

  it('never offers a delisted symbol the lake does not already hold', () => {
    alpaca.entries.set([
      {
        symbol: 'AAPL',
        name: 'Apple Inc.',
        asset_class: 'us_equity',
        exchange: 'NASDAQ',
        status: 'active',
      },
      {
        symbol: 'OLD',
        name: 'Delisted Corp',
        asset_class: 'us_equity',
        exchange: 'NYSE',
        status: 'inactive',
      },
    ]);
    fixture.detectChanges();
    openDropdown(fixture);

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('AAPL');
    expect(text).not.toContain('Delisted Corp');
  });

  it('badges a held symbol the vendor has delisted, and keeps it pickable', () => {
    catalog.view.pool.set([
      {
        symbol: 'OLD',
        name: 'Delisted Corp',
        exchange: 'NYSE',
        firstHeld: '2019-01-02',
        lastHeld: '2020-01-31',
      },
    ]);
    alpaca.entries.set([
      {
        symbol: 'OLD',
        name: 'Delisted Corp',
        asset_class: 'us_equity',
        exchange: 'NYSE',
        status: 'inactive',
      },
    ]);
    fixture.detectChanges();
    openDropdown(fixture);

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('OLD');
    expect(text).toContain('delisted');
  });

  it('shows a banner — not a blank list — when the live catalog is dark', () => {
    alpaca.unavailable.set('The broker catalog is unreachable.');
    fixture.detectChanges();
    openDropdown(fixture);

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Live catalog unavailable');
    // Degraded, not empty: the lake's own answer still stands.
    expect(text).toContain('SPY');
  });

  it('does not claim to show lake holdings when neither source answered', () => {
    catalog.view.pool.set([]);
    catalog.view.unavailable.set('The data lake is unreachable.');
    alpaca.unavailable.set('The live catalog is unreachable.');
    fixture.detectChanges();
    openDropdown(fixture);

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('The data lake is unreachable.');
    expect(text).not.toContain('showing lake holdings');
  });

  it('says why the list is empty when the lake did not answer, and can retry', () => {
    catalog.view.pool.set([]);
    catalog.view.unavailable.set('The data lake is unreachable.');
    fixture.detectChanges();
    openDropdown(fixture);

    expect(fixture.nativeElement.textContent).toContain('The data lake is unreachable.');

    const retry: HTMLButtonElement | null = fixture.nativeElement.querySelector('.dropdown__retry');
    expect(retry).not.toBeNull();
    // Delta, not an absolute: opening the dropdown re-reads too.
    const before = catalog.view.reloadCount;
    retry?.click();
    expect(catalog.view.reloadCount).toBe(before + 1);
  });

  it.each([
    [
      // Sidecar validator rejects weekend endpoints; pickTicker derives
      // ``from = lastHeld - 30 days`` which lands on a weekend whenever
      // ``lastHeld`` is Mon-Wed. Guard both endpoints.
      // Held days start after the window on screen (2025-04), so the window is
      // retargeted: lastHeld = Mon 2026-05-25 → −30 = Sat 2026-04-25 → walks to
      // Fri 2026-04-24. ``to`` is the supplied weekday Mon (no walk).
      'bumps a weekend-derived from date back to Friday',
      { symbol: 'AAPL', name: 'Apple', exchange: 'NASDAQ', firstHeld: '2026-04-01', lastHeld: '2026-05-25' },
      '2026-04-24',
      '2026-05-25',
    ],
    [
      // lastHeld = Wed 2026-10-07 → −30 = Mon 2026-09-07, Labor Day → Fri 2026-09-04.
      // A weekday walk would keep the holiday, and the date picker refuses it.
      'proposes a window on sessions: a start on a holiday steps back to the session before',
      { symbol: 'AAPL', name: 'Apple', exchange: 'NASDAQ', firstHeld: '2026-04-01', lastHeld: '2026-10-07' },
      '2026-09-04',
      '2026-10-07',
    ],
    [
      // The lake holds three days of STRL. A blind ``lastHeld - 30`` would open
      // on 2026-04-25, four weeks of which has no bars to read.
      'clamps the proposed window to the first day held',
      { symbol: 'STRL', name: 'Sterling Infrastructure, Inc.', exchange: 'NASDAQ', firstHeld: '2026-05-21', lastHeld: '2026-05-25' },
      '2026-05-21',
      '2026-05-25',
    ],
  ])('pickTicker %s', (_name, ticker, from, to) => {
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker(ticker);
    fixture.detectChanges();

    expect(component.value().from).toBe(from);
    expect(component.value().to).toBe(to);
  });

  it('pickTicker steps back off weekends on its own while the calendar has not been read', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      imports: [InstrumentCardComponent],
      providers: [
        provideRouter([]),
        provideFakeTickerCatalog(catalog),
        provideFakeVendorCatalog(alpaca),
        provideFakeEnsureCoverage(coverage),
        { provide: TradingCalendarService, useValue: fakeTradingCalendar(null, '2026-12-01') },
      ],
    });
    const unread = TestBed.createComponent(InstrumentCardComponent);
    unread.componentRef.setInput('value', baseValue);
    unread.detectChanges();

    unread.componentInstance.pickTicker({ symbol: 'AAPL', name: 'Apple', exchange: 'NASDAQ', firstHeld: '2026-04-01', lastHeld: '2026-05-25' });

    // −30 = Sat 2026-04-25 → Fri 2026-04-24.
    expect(unread.componentInstance.value().from).toBe('2026-04-24');
  });

  it('leaves the window alone for a host that has none', () => {
    fixture.componentRef.setInput('movesWindow', false);
    fixture.detectChanges();

    component.pickTicker({ symbol: 'AAPL', name: 'Apple', exchange: 'NASDAQ', firstHeld: '2026-04-01', lastHeld: '2026-10-07' });

    expect(component.value()).toMatchObject({ symbol: 'AAPL', from: baseValue.from, to: baseValue.to });
  });

  // An unheld pick is no longer a dead end: the gate backfills it, and only
  // a covered symbol reaches value(). This is the populate-then-use loop.
  it('gates an unheld pick on its backfill and selects it once covered', async () => {
    coverage.ensureResults.push(true);
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker({
      symbol: 'NVDA',
      name: 'NVIDIA',
      exchange: 'NASDAQ',
    });
    await flushGate(fixture);

    expect(coverage.ensureCalls).toEqual([{ symbol: 'NVDA', mode: 'polygon_split_adjusted' }]);
    expect(component.value().symbol).toBe('NVDA');
  });

  it('leaves the selection untouched when the backfill fails, showing the strip', async () => {
    // A false verdict hands the card a failed strip, as the real service does.
    coverage.ensureResults.push(false);
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker({
      symbol: 'NVDA',
      name: 'NVIDIA',
      exchange: 'NASDAQ',
    });
    await flushGate(fixture);

    expect(component.value().symbol).toBe('SPY');
    const strip: HTMLElement | null = fixture.nativeElement.querySelector(
      'app-coverage-gate-strip .gate',
    );
    expect(strip).not.toBeNull();
    expect(strip?.textContent).toContain('The backfill job failed.');
    // The failure's reason code renders through the receipt-label pipe.
    expect(strip?.querySelector('.gate__msg .mono')).not.toBeNull();
    // Cancel is offered so the operator can walk away cleanly.
    expect(strip?.textContent).toContain('Retry');
    expect(strip?.textContent).toContain('Dismiss');
  });

  it('refuses the gate while the coverage read is still loading — vendor rows carry no verdict yet', () => {
    // The vendor catalog answering before the lake's first summary is the
    // exact window where unheld-looking rows render with no held verdict.
    seedListedUnheld();
    catalog.viewFor('polygon_split_adjusted').loading.set(true);
    fixture.detectChanges();
    openDropdown(fixture);

    const option = Array.from(fixture.nativeElement.querySelectorAll('[role="option"]')).find(
      (candidate) => (candidate as HTMLElement).textContent?.includes('TSLA'),
    ) as HTMLElement;
    option.click();
    fixture.detectChanges();

    expect(coverage.refusals).toContainEqual(
      expect.objectContaining({
        symbol: 'TSLA',
        reason: 'coverage_unknown',
        message: 'The lake coverage read is still in flight.',
      }),
    );
    expect(coverage.ensureCalls).toEqual([]);
  });

  it('a host-driven symbol replacement disarms the pending gate', async () => {
    seedListedUnheld();
    fixture.detectChanges();
    openDropdown(fixture);

    coverage.holdNext = true;
    const option = Array.from(fixture.nativeElement.querySelectorAll('[role="option"]')).find(
      (candidate) => (candidate as HTMLElement).textContent?.includes('TSLA'),
    ) as HTMLElement;
    option.click();
    fixture.detectChanges();
    const held = coverage.held[0];

    // The host replaces the symbol (a strategy switch seeding QQQ) while
    // the backfill runs.
    fixture.componentRef.setInput('value', { ...baseValue, symbol: 'QQQ' });
    fixture.detectChanges();
    expect(held.cancelCalls).toBe(1);

    // Even a successful backfill must not overwrite the newer symbol.
    held.resolve(true);
    await flushGate(fixture);
    expect(component.value().symbol).toBe('QQQ');
  });

  it('gives each card its own listbox id — two cards on a page stay separate comboboxes', () => {
    fixture.detectChanges();
    openDropdown(fixture);
    const second = TestBed.createComponent(InstrumentCardComponent);
    second.componentRef.setInput('value', { ...baseValue, symbol: 'QQQ' });
    second.detectChanges();
    openDropdown(second);

    const boxes = [
      ...fixture.nativeElement.querySelectorAll('[role="combobox"]'),
      ...second.nativeElement.querySelectorAll('[role="combobox"]'),
    ] as HTMLElement[];
    const listboxes = [
      ...fixture.nativeElement.querySelectorAll('[role="listbox"]'),
      ...second.nativeElement.querySelectorAll('[role="listbox"]'),
    ] as HTMLElement[];
    expect(boxes.length).toBe(2);
    const controls = boxes.map((box) => box.getAttribute('aria-controls'));
    expect(new Set(controls).size).toBe(2);
    expect(listboxes.map((list) => list.id).sort()).toEqual([...controls].sort());
  });

  it('refuses the gate loudly for a view no backfill can produce', () => {
    fixture.componentRef.setInput('adjustmentMode', 'lean_adjusted');
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker({
      symbol: 'NVDA',
      name: 'NVIDIA',
      exchange: 'NASDAQ',
    });
    fixture.detectChanges();

    expect(coverage.ensureCalls).toEqual([]);
    expect(coverage.refusals).toEqual([
      {
        symbol: 'NVDA',
        mode: 'lean_adjusted',
        reason: 'view_not_backfillable',
        // The refusal names the view the way the strip renders it.
        message: expect.stringContaining('Lean Adjusted'),
      },
    ]);
    expect(component.value().symbol).toBe('SPY');
  });

  it('dismisses the gate strip without selecting', async () => {
    coverage.ensureResults.push(false);
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();
    component.pickTicker({
      symbol: 'NVDA',
      name: 'NVIDIA',
      exchange: 'NASDAQ',
    });
    await flushGate(fixture);

    const buttons = fixture.nativeElement.querySelectorAll('app-coverage-gate-strip .gate button');
    const dismiss = Array.from(buttons).find(
      (b) => (b as HTMLButtonElement).textContent?.trim() === 'Dismiss',
    ) as HTMLButtonElement | undefined;
    dismiss?.click();
    await flushGate(fixture);

    expect((component.gate.pendingSession() as FakeCoverageSession | null) ?? null).toBeNull();
    expect(fixture.nativeElement.querySelector('app-coverage-gate-strip')).toBeNull();
  });

  it('shows the read in flight rather than a stale failure while retrying', () => {
    catalog.view.pool.set([]);
    catalog.view.unavailable.set('The data lake is unreachable.');
    fixture.detectChanges();
    openDropdown(fixture);

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
    openDropdown(fixture);

    const listbox: HTMLElement | null = fixture.nativeElement.querySelector('[role="listbox"]');
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
      {
        symbol: 'TSLA',
        name: 'Tesla, Inc.',
        asset_class: 'us_equity',
        exchange: 'NASDAQ',
        status: 'active',
      },
    ]);
    fixture.componentRef.setInput('universe', [
      { symbol: 'QQQ', name: 'Invesco QQQ Trust', exchange: 'NASDAQ' },
    ]);
    fixture.detectChanges();
    openDropdown(fixture);

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
      {
        symbol: 'RAWONLY',
        name: 'Raw-tree only',
        exchange: 'ARCA',
        lastHeld: '2026-09-04',
      },
    ]);
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.detectChanges();
    openDropdown(fixture);

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('RAWONLY');
    expect(text).not.toContain('QQQ');
    expect(catalog.modesRequested).toContain('raw');
  });

  it('does not re-read the lake for a host-supplied universe', () => {
    fixture.componentRef.setInput('universe', [
      { symbol: 'QQQ', name: 'Invesco QQQ Trust', exchange: 'NASDAQ' },
    ]);
    fixture.detectChanges();
    const before = catalog.view.reloadCount;
    openDropdown(fixture);

    expect(catalog.view.reloadCount).toBe(before);
    expect(alpaca.reloadCount).toBe(before);
  });

  // A gate in flight must not speak for value() after the operator has
  // moved on: the held pick wins immediately, the superseded gate is
  // abandoned, and its eventual completion changes nothing. Ownership is
  // the session itself — abandoning cancels exactly this card's gate.
  it('a held pick made while a gate is in flight wins over the finished gate', async () => {
    coverage.holdNext = true;
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker({
      symbol: 'NVDA',
      name: 'NVIDIA',
      exchange: 'NASDAQ',
    });
    await flushGate(fixture);
    const heldGate = component.gate.pendingSession() as FakeCoverageSession;
    expect(heldGate).not.toBeNull();

    component.pickTicker(pool[0]);
    fixture.detectChanges();

    expect(component.value().symbol).toBe('SPY');
    expect(heldGate.cancelCalls).toBe(1);

    heldGate.resolve(true);
    await flushGate(fixture);
    expect(component.value().symbol).toBe('SPY');
  });

  it('abandons an old-tree gate when the host changes adjustment mode', async () => {
    coverage.holdNext = true;
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker({
      symbol: 'NVDA',
      name: 'NVIDIA',
      exchange: 'NASDAQ',
    });
    await flushGate(fixture);
    const adjustedGate = component.gate.pendingSession() as FakeCoverageSession;

    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.detectChanges();

    expect(adjustedGate.cancelCalls).toBe(1);
    expect(component.gate.pendingSession()).toBeNull();
    adjustedGate.resolve(true);
    await flushGate(fixture);
    expect(component.value().symbol).toBe('SPY');
  });

  it('releases a pending gate when the card is destroyed', async () => {
    coverage.holdNext = true;
    fixture.detectChanges();
    component.pickTicker({
      symbol: 'NVDA',
      name: 'NVIDIA',
      exchange: 'NASDAQ',
    });
    await flushGate(fixture);
    const session = component.gate.pendingSession() as FakeCoverageSession;

    fixture.destroy();

    expect(session.cancelCalls).toBe(1);
  });

  it('caps the rendered rows and lets the search reach the rest', () => {
    const many: VendorSymbolEntry[] = Array.from({ length: 80 }, (_, i) => ({
      symbol: `SYM${i}`,
      name: `Symbol ${i}`,
      asset_class: 'us_equity',
      exchange: 'NASDAQ',
      status: 'active',
    }));
    alpaca.entries.set(many);
    fixture.detectChanges();
    openDropdown(fixture);
    component.onSearchInput('SYM');
    fixture.detectChanges();

    // 80 vendor matches, 50 rendered: the search is the scaler past the cap.
    const rows = fixture.nativeElement.querySelectorAll('.dropdown__scroll .row');
    expect(rows.length).toBe(50);
    // The header count stays honest about what the search covers.
    expect(fixture.nativeElement.textContent).toContain('80 matches');
  });

  it('lists the typed symbol above the matches that sort ahead of it', () => {
    alpaca.entries.set([
      { symbol: 'BGLD', name: 'FT Vest Gold Strategy Quarterly Buffer ETF', asset_class: 'us_equity', exchange: 'BATS', status: 'active' },
      { symbol: 'GLD', name: 'SPDR Gold Shares', asset_class: 'us_equity', exchange: 'ARCA', status: 'active' },
    ]);
    fixture.detectChanges();
    openDropdown(fixture);
    component.onSearchInput('GLD');
    fixture.detectChanges();

    const listed = Array.from(
      fixture.nativeElement.querySelectorAll('.dropdown__scroll .row .asset-identity__symbol'),
    ).map((cell) => (cell as HTMLElement).textContent?.trim());
    expect(listed).toEqual(['GLD', 'BGLD']);
  });

  // With the lake dark, coverage is unknown — every row reads unheld and a
  // gate would backfill on a guess. The pick is refused with the lake's own
  // reason instead.
  it('refuses the gate while the lake coverage verdict is unknown', async () => {
    catalog.view.unavailable.set('The data lake is unreachable.');
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker({
      symbol: 'NVDA',
      name: 'NVIDIA',
      exchange: 'NASDAQ',
    });
    await flushGate(fixture);

    expect(coverage.ensureCalls).toEqual([]);
    expect(coverage.refusals).toEqual([
      {
        symbol: 'NVDA',
        mode: 'polygon_split_adjusted',
        reason: 'coverage_unknown',
        message: 'The data lake is unreachable.',
      },
    ]);
    expect(component.value().symbol).toBe('SPY');
  });

  it('offers no Retry for a refusal retrying cannot fix', async () => {
    fixture.componentRef.setInput('adjustmentMode', 'lean_adjusted');
    fixture.detectChanges();
    component.openDropdown();
    fixture.detectChanges();

    component.pickTicker({
      symbol: 'NVDA',
      name: 'NVIDIA',
      exchange: 'NASDAQ',
    });
    await flushGate(fixture);

    const labels = Array.from(
      fixture.nativeElement.querySelectorAll('app-coverage-gate-strip .gate button'),
    ).map((b) => (b as HTMLButtonElement).textContent?.trim());
    expect(labels).toContain('Dismiss');
    expect(labels).not.toContain('Retry');
  });
});
