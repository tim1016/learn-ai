import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { InstrumentCardComponent } from './instrument-card.component';
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
      cache: 0.95,
      last: '2025-04-30',
    },
    {
      symbol: 'QQQ',
      name: 'Invesco QQQ',
      exchange: 'NASDAQ',
      cache: 0.8,
      last: '2025-04-30',
    },
  ];

  let fixture: ComponentFixture<InstrumentCardComponent>;
  let component: InstrumentCardComponent;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [InstrumentCardComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(InstrumentCardComponent);
    component = fixture.componentInstance;

    fixture.componentRef.setInput('value', baseValue);
    fixture.componentRef.setInput('tickerPool', pool);
    fixture.componentRef.setInput('recent', []);
  });

  it('renders the current symbol and exchange', () => {
    fixture.detectChanges();
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('SPY');
    expect(text).toContain('ARCA');
  });

  it('opens the dropdown on click and shows the recent list when query is empty', () => {
    fixture.componentRef.setInput('recent', ['QQQ']);
    fixture.detectChanges();

    const tickerBox: HTMLElement | null =
      fixture.nativeElement.querySelector('[role="combobox"]');
    expect(tickerBox).not.toBeNull();
    tickerBox!.click();
    fixture.detectChanges();

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

  it('selectedTickerCachePct exposes the matched pool entry cache', () => {
    fixture.detectChanges();
    expect(component.selectedTickerCachePct()).toBe(0.95);
  });

  it('selectedTickerLast exposes the matched pool entry last date', () => {
    fixture.detectChanges();
    expect(component.selectedTickerLast()).toBe('2025-04-30');
  });
});
