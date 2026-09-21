import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { MultiInstrumentCardComponent } from './multi-instrument-card.component';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../ticker-catalog/testing/fake-ticker-catalog';
import type { MultiTickerRange } from './multi-ticker-range-picker.types';

describe('MultiInstrumentCardComponent', () => {
  const baseValue: MultiTickerRange = {
    symbols: ['SPY'],
    from: '2025-04-01',
    to: '2025-04-30',
    resolution: 'minute',
  };
  const pool: TickerOption[] = [
    { symbol: 'SPY', name: 'SPDR S&P 500' },
    { symbol: 'QQQ', name: 'Invesco QQQ' },
    { symbol: 'IWM', name: 'iShares Russell 2000' },
  ];

  let fixture: ComponentFixture<MultiInstrumentCardComponent>;
  let component: MultiInstrumentCardComponent;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      providers: [provideFakeTickerCatalog(fakeTickerCatalog(pool))],
      imports: [MultiInstrumentCardComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(MultiInstrumentCardComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('value', baseValue);
  });

  it('renders one chip per selected symbol', () => {
    fixture.componentRef.setInput('value', { ...baseValue, symbols: ['SPY', 'QQQ'] });
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelectorAll('.chip').length).toBe(2);
  });

  it('add() appends to symbols and clears the query', () => {
    fixture.detectChanges();
    component.query.set('Q');
    component.add('QQQ');
    expect(component.value().symbols).toEqual(['SPY', 'QQQ']);
    expect(component.query()).toBe('');
  });

  it('add() is idempotent — adding an already-selected symbol is a no-op', () => {
    fixture.detectChanges();
    component.add('SPY');
    expect(component.value().symbols).toEqual(['SPY']);
  });

  it('remove() drops a symbol but refuses to leave the array empty', () => {
    fixture.componentRef.setInput('value', { ...baseValue, symbols: ['SPY', 'QQQ'] });
    fixture.detectChanges();
    component.remove('QQQ');
    expect(component.value().symbols).toEqual(['SPY']);

    component.remove('SPY');
    // Refuses — keeps SPY selected.
    expect(component.value().symbols).toEqual(['SPY']);
  });

  it('selectAll() picks every pool symbol', () => {
    fixture.detectChanges();
    component.selectAll();
    expect(component.value().symbols).toEqual(['SPY', 'QQQ', 'IWM']);
  });

  it('selectNone() leaves the first pool symbol selected', () => {
    fixture.componentRef.setInput('value', { ...baseValue, symbols: ['SPY', 'QQQ'] });
    fixture.detectChanges();
    component.selectNone();
    expect(component.value().symbols).toEqual(['SPY']);
  });

  // The backfill panel's host universe is the vendor catalog: "None" must
  // mean none, not "an arbitrary symbol from an alphabetically sorted
  // universe of thousands", which a submit would then backfill.
  it('selectNone() empties a host universe and remove() may empty it too', () => {
    fixture.componentRef.setInput('universe', pool);
    fixture.componentRef.setInput('value', { ...baseValue, symbols: ['SPY', 'QQQ'] });
    fixture.detectChanges();

    component.selectNone();
    expect(component.value().symbols).toEqual([]);

    component.add('IWM');
    component.remove('IWM');
    expect(component.value().symbols).toEqual([]);
  });

  it('labels an empty host universe as a catalog, not the lake', () => {
    fixture.componentRef.setInput('universe', []);
    fixture.detectChanges();

    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('The catalog is empty.');
    expect(text).not.toContain('The data lake holds no instruments yet.');
  });

  it('addable filters out already-selected symbols', () => {
    fixture.componentRef.setInput('value', { ...baseValue, symbols: ['SPY'] });
    fixture.detectChanges();
    expect(component.addable().map((t) => t.symbol)).toEqual(['QQQ', 'IWM']);
  });
});
