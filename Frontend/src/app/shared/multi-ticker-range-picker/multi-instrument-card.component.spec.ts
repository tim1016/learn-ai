import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { MultiInstrumentCardComponent } from './multi-instrument-card.component';
import type { PickerSymbol } from '../symbol-catalog/symbol-catalog.types';

describe('MultiInstrumentCardComponent', () => {
  const options: PickerSymbol[] = [
    { symbol: 'SPY', name: 'SPDR S&P 500', delisted: false },
    { symbol: 'QQQ', name: 'Invesco QQQ', delisted: false },
    { symbol: 'IWM', name: 'iShares Russell 2000', delisted: false },
  ];

  let fixture: ComponentFixture<MultiInstrumentCardComponent>;
  let component: MultiInstrumentCardComponent;

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
    await TestBed.configureTestingModule({
      imports: [MultiInstrumentCardComponent],
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
});
