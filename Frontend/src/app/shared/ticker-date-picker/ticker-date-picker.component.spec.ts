import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { TickerDatePickerComponent } from './ticker-date-picker.component';
import type { TickerSnapshot } from './ticker-date-picker.types';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';

describe('TickerDatePickerComponent', () => {
  const baseValue: TickerSnapshot = { symbol: 'SPY', date: '2025-04-30' };
  const pool: TickerOption[] = [
    { symbol: 'SPY', name: 'SPDR S&P 500' },
    { symbol: 'AAPL', name: 'Apple' },
  ];

  let fixture: ComponentFixture<TickerDatePickerComponent>;
  let component: TickerDatePickerComponent;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [TickerDatePickerComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(TickerDatePickerComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('value', baseValue);
    fixture.componentRef.setInput('tickerPool', pool);
  });

  it('renders the symbol and date in the summary line', () => {
    fixture.detectChanges();
    const text: string = fixture.nativeElement.textContent ?? '';
    expect(text).toContain('SPY');
    expect(text).toContain('2025-04-30');
  });

  it('composes the InstrumentCard sub-component', () => {
    fixture.detectChanges();
    expect(
      fixture.nativeElement.querySelector('app-instrument-card'),
    ).not.toBeNull();
  });

  it('rangeProjection() collapses from/to to the snapshot date', () => {
    fixture.detectChanges();
    const projection = (
      component as unknown as { rangeProjection: { (): { from: string; to: string } } }
    ).rangeProjection();
    expect(projection.from).toBe('2025-04-30');
    expect(projection.to).toBe('2025-04-30');
  });

  it('onInstrumentPatch only updates the symbol, ignoring range fields', () => {
    fixture.detectChanges();
    (component as unknown as {
      onInstrumentPatch: (r: {
        symbol: string;
        from: string;
        to: string;
        resolution: string;
      }) => void;
    }).onInstrumentPatch({
      symbol: 'AAPL',
      from: '2025-03-01',
      to: '2025-04-30',
      resolution: 'daily',
    });
    expect(component.value().symbol).toBe('AAPL');
    expect(component.value().date).toBe('2025-04-30');
  });

  it('onDateChange formats local-midnight Date back to YYYY-MM-DD', () => {
    fixture.detectChanges();
    (component as unknown as {
      onDateChange: (d: Date | null) => void;
    }).onDateChange(new Date(2025, 5, 21));
    expect(component.value().date).toBe('2025-06-21');
  });

  it('dateValue parses YYYY-MM-DD back to a local-midnight Date', () => {
    fixture.detectChanges();
    const d = (component as unknown as { dateValue: Date | null }).dateValue;
    expect(d).not.toBeNull();
    expect(d!.getFullYear()).toBe(2025);
    expect(d!.getMonth()).toBe(3);
    expect(d!.getDate()).toBe(30);
  });
});
