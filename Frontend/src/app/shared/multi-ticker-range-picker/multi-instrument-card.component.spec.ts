import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { flushGate } from '../symbol-picker/testing/fake-picker-world';
import { MultiInstrumentCardComponent } from './multi-instrument-card.component';
import type { PickerSymbol } from '../symbol-catalog/symbol-catalog.types';
import {
  fakeEnsureCoverage,
  provideFakeEnsureCoverage,
  type FakeEnsureCoverage,
} from '../symbol-catalog/testing/fake-symbol-catalog';

describe('MultiInstrumentCardComponent', () => {
  const options: PickerSymbol[] = [
    { symbol: 'SPY', name: 'SPDR S&P 500', delisted: false, firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
    { symbol: 'QQQ', name: 'Invesco QQQ', delisted: false },
    { symbol: 'IWM', name: 'iShares Russell 2000', delisted: false },
  ];

  let fixture: ComponentFixture<MultiInstrumentCardComponent>;
  let component: MultiInstrumentCardComponent;
  let coverage: FakeEnsureCoverage;

  function setInputSymbols(symbols: string[]): void {
    fixture.componentRef.setInput('symbols', symbols);
  }

  function search(value: string): HTMLInputElement {
    const input = fixture.nativeElement.querySelector(
      '[aria-label="Search to add a ticker"]',
    ) as HTMLInputElement;
    input.value = value;
    input.dispatchEvent(new Event('input'));
    fixture.detectChanges();
    return input;
  }


  beforeEach(async () => {
    TestBed.resetTestingModule();
    coverage = fakeEnsureCoverage();
    await TestBed.configureTestingModule({
      imports: [MultiInstrumentCardComponent],
      providers: [provideFakeEnsureCoverage(coverage)],
    }).compileComponents();

    fixture = TestBed.createComponent(MultiInstrumentCardComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('options', options);
    setInputSymbols(['SPY']);
  });

  it('renders one chip per selected symbol', () => {
    setInputSymbols(['SPY', 'QQQ']);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('.chip').length).toBe(2);
  });

  it('adds a searched symbol and clears the query', () => {
    fixture.detectChanges();
    const input = search('Q');
    const option = Array.from(fixture.nativeElement.querySelectorAll('[role="option"]')).find(
      (candidate) => (candidate as HTMLElement).textContent?.includes('QQQ'),
    ) as HTMLButtonElement | undefined;
    option?.click();
    fixture.detectChanges();

    expect(component.symbols()).toEqual(['SPY', 'QQQ']);
    expect(input.value).toBe('');
  });

  it('add() is idempotent — adding an already-selected symbol is a no-op', () => {
    fixture.detectChanges();
    component.add('SPY');
    expect(component.symbols()).toEqual(['SPY']);
  });

  it('remove() drops a symbol but refuses to leave the array empty by default', () => {
    setInputSymbols(['SPY', 'QQQ']);
    fixture.detectChanges();
    component.remove('QQQ');
    expect(component.symbols()).toEqual(['SPY']);

    component.remove('SPY');
    // Refuses — keeps SPY selected: the payload stays valid against a
    // `min_length=1` request model.
    expect(component.symbols()).toEqual(['SPY']);
  });

  it('selectAll() picks every option', () => {
    fixture.detectChanges();
    component.selectAll();
    expect(component.symbols()).toEqual(['SPY', 'QQQ', 'IWM']);
  });

  it('selectNone() leaves the first option selected by default', () => {
    setInputSymbols(['SPY', 'QQQ']);
    fixture.detectChanges();
    component.selectNone();
    expect(component.symbols()).toEqual(['SPY']);
  });

  // "None" must mean none over a universe of thousands — keeping options[0]
  // would quietly nominate an arbitrary symbol, which a submit would then
  // backfill.
  it('selectNone() empties and remove() may empty it too when the host allows', () => {
    fixture.componentRef.setInput('allowEmpty', true);
    setInputSymbols(['SPY', 'QQQ']);
    fixture.detectChanges();

    component.selectNone();
    expect(component.symbols()).toEqual([]);

    component.add('IWM');
    component.remove('IWM');
    expect(component.symbols()).toEqual([]);
  });

  it('says when nothing is on offer', () => {
    fixture.componentRef.setInput('options', []);
    fixture.detectChanges();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('No instruments available.');
  });

  it('shows an unavailable note with a retry the host wired', () => {
    fixture.componentRef.setInput('unavailable', 'The lake is unreachable.');
    fixture.detectChanges();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('The lake is unreachable.');
    const retry = Array.from(fixture.nativeElement.querySelectorAll('button')).find((b) =>
      (b as HTMLButtonElement).textContent?.includes('Retry'),
    );
    expect(retry).not.toBeNull();
  });

  it('search results filter out already-selected symbols', () => {
    setInputSymbols(['SPY']);
    fixture.detectChanges();
    search('S');

    const optionsText = Array.from(fixture.nativeElement.querySelectorAll('[role="option"]')).map(
      (candidate) => (candidate as HTMLElement).textContent ?? '',
    );
    expect(optionsText.some((text) => text.includes('SPY'))).toBe(false);
    expect(optionsText.some((text) => text.includes('IWM'))).toBe(true);
  });
  // ── The optional coverage gate (ADR 0066) ────────────────────────────────

  it('with an adjustment mode, a held pick applies immediately — no gate', () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.detectChanges();

    component.add('SPY');
    component.add('SPY'); // idempotent

    expect(coverage.ensureCalls).toEqual([]);
    expect(component.symbols()).toEqual(['SPY']);
  });

  it('gates an unheld pick: the chip appears only once the lake confirms coverage', async () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.detectChanges();

    component.add('QQQ'); // vendor-only row — no held span
    fixture.detectChanges();

    expect(coverage.ensureCalls).toEqual([{ symbol: 'QQQ', mode: 'raw' }]);
    expect(component.symbols()).toEqual(['SPY']); // populate-then-use

    await flushGate(fixture);
    expect(component.symbols()).toEqual(['SPY', 'QQQ']);
  });

  it('renders the gate strip while the gate runs, and can dismiss it', async () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.detectChanges();

    coverage.holdNext = true;
    component.add('QQQ');
    fixture.detectChanges();

    const strip = fixture.nativeElement.querySelector('app-coverage-gate-strip');
    expect(strip).not.toBeNull();
    expect(component.symbols()).toEqual(['SPY']);

    const held = coverage.held[0];
    held.cancel();
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('app-coverage-gate-strip')).toBeNull();
    expect(component.symbols()).toEqual(['SPY']);
  });

  it('refuses the gate while the lake verdict is unknown', () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.componentRef.setInput('unavailable', 'The lake is unreachable.');
    fixture.detectChanges();

    component.add('QQQ');

    expect(coverage.refusals).toContainEqual(
      expect.objectContaining({ symbol: 'QQQ', reason: 'coverage_unknown' }),
    );
    expect(coverage.ensureCalls).toEqual([]);
    expect(component.symbols()).toEqual(['SPY']);
  });

  it('refuses the gate loudly for a view no backfill can produce', () => {
    fixture.componentRef.setInput('adjustmentMode', 'lean_adjusted');
    fixture.detectChanges();

    component.add('QQQ');

    expect(coverage.refusals).toContainEqual(
      expect.objectContaining({ symbol: 'QQQ', reason: 'view_not_backfillable' }),
    );
    expect(component.symbols()).toEqual(['SPY']);
  });

  it('releases a pending gate when the host changes the adjustment mode', () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.detectChanges();

    coverage.holdNext = true;
    component.add('QQQ');
    fixture.detectChanges();
    const held = coverage.held[0];

    fixture.componentRef.setInput('adjustmentMode', 'polygon_split_adjusted');
    fixture.detectChanges();

    expect(held.cancelCalls).toBe(1);
    expect(component.symbols()).toEqual(['SPY']);
  });

  it('never offers All on a gated card — unheld symbols backfill one at a time', () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.detectChanges();

    component.selectAll();
    expect(component.symbols()).toEqual(['SPY']);
  });

  it('retries a coverage-unknown refusal by re-asking the lake, not by skipping the check', () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.componentRef.setInput('unavailable', 'The lake is unreachable.');
    fixture.detectChanges();

    component.add('QQQ');
    expect(coverage.refusals).toContainEqual(
      expect.objectContaining({ symbol: 'QQQ', reason: 'coverage_unknown' }),
    );
    expect(coverage.ensureCalls).toEqual([]);

    // The lake recovers; the strip's Retry re-runs the admission — which now
    // starts a real gate instead of replaying the refusal.
    fixture.componentRef.setInput('unavailable', null);
    fixture.detectChanges();

    // The strip's own Retry button — the operator's path.
    const retry = Array.from(fixture.nativeElement.querySelectorAll('button')).find((b) =>
      (b as HTMLButtonElement).textContent?.trim() === 'Retry',
    ) as HTMLButtonElement;
    expect(retry).not.toBeNull();
    retry.click();
    fixture.detectChanges();

    expect(coverage.ensureCalls).toEqual([{ symbol: 'QQQ', mode: 'raw' }]);
  });

  it('refuses admission while the coverage read is still loading — no gate on a guess', () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.componentRef.setInput('loading', true);
    fixture.detectChanges();

    component.add('QQQ');

    expect(coverage.refusals).toContainEqual(
      expect.objectContaining({
        symbol: 'QQQ',
        reason: 'coverage_unknown',
        message: 'The lake coverage read is still in flight.',
      }),
    );
    expect(coverage.ensureCalls).toEqual([]);
    expect(component.symbols()).toEqual(['SPY']);
  });

  it('a cleared selection cancels the pending gate — a finished backfill cannot resurrect it', async () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.componentRef.setInput('allowEmpty', true);
    fixture.detectChanges();

    coverage.holdNext = true;
    component.add('QQQ');
    fixture.detectChanges();
    const held = coverage.held[0];

    component.selectNone();
    fixture.detectChanges();
    expect(held.cancelCalls).toBe(1);

    held.resolve(true);
    await flushGate(fixture);
    expect(component.symbols()).toEqual([]);
  });

  it('a host-driven selection replacement voids the pending pick — no resurrect after a reseed', async () => {
    fixture.componentRef.setInput('adjustmentMode', 'raw');
    fixture.detectChanges();

    coverage.holdNext = true;
    component.add('QQQ');
    fixture.detectChanges();
    const held = coverage.held[0];

    // The host rebinds a fresh selection (a URL reseed, a preset) while the
    // backfill runs.
    setInputSymbols(['SPY']);
    fixture.detectChanges();

    held.resolve(true);
    await flushGate(fixture);
    expect(component.symbols()).toEqual(['SPY']);
  });

  it('shows the degraded banner with a vendor retry the host wired', () => {
    fixture.componentRef.setInput('degraded', 'catalog endpoint down');
    fixture.detectChanges();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('Live catalog unavailable');
    expect(text).toContain('showing lake holdings');
    expect(text).toContain('catalog endpoint down');
    const retry = Array.from(fixture.nativeElement.querySelectorAll('button')).find((b) =>
      (b as HTMLButtonElement).textContent?.includes('Retry'),
    );
    expect(retry).not.toBeNull();
  });
});
