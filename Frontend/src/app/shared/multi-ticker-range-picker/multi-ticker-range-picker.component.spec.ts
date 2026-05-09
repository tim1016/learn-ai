import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { MultiTickerRangePickerComponent } from './multi-ticker-range-picker.component';
import type { TickerOption } from '../ticker-range-picker/ticker-range-picker.types';
import type { MultiTickerRange } from './multi-ticker-range-picker.types';

describe('MultiTickerRangePickerComponent', () => {
  const baseValue: MultiTickerRange = {
    symbols: ['SPY'],
    from: '2025-04-01',
    to: '2025-04-30',
    resolution: 'minute',
  };
  const pool: TickerOption[] = [
    { symbol: 'SPY', name: 'SPDR S&P 500' },
    { symbol: 'QQQ', name: 'Invesco QQQ' },
  ];

  let fixture: ComponentFixture<MultiTickerRangePickerComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [MultiTickerRangePickerComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(MultiTickerRangePickerComponent);
    fixture.componentRef.setInput('value', baseValue);
    fixture.componentRef.setInput('tickerPool', pool);
  });

  it('composes the three sub-components', () => {
    fixture.detectChanges();
    expect(
      fixture.nativeElement.querySelector('app-multi-instrument-card'),
    ).not.toBeNull();
    expect(
      fixture.nativeElement.querySelector('app-time-window-card'),
    ).not.toBeNull();
    expect(
      fixture.nativeElement.querySelector('app-sampling-card'),
    ).not.toBeNull();
  });

  it('hideSampling=true collapses the Sampling card', () => {
    fixture.componentRef.setInput('hideSampling', true);
    fixture.detectChanges();
    expect(
      fixture.nativeElement.querySelector('app-sampling-card'),
    ).toBeNull();
  });

  it('passes availableMultipliers through to SamplingCard', () => {
    fixture.componentRef.setInput('availableMultipliers', [1, 5, 15]);
    fixture.detectChanges();
    expect(
      fixture.nativeElement.querySelector('.multiplier__select'),
    ).not.toBeNull();
  });

  it('renders the universe count in the summary line', () => {
    fixture.componentRef.setInput('value', {
      ...baseValue,
      symbols: ['SPY', 'QQQ', 'IWM'],
    });
    fixture.detectChanges();
    expect(fixture.nativeElement.textContent).toContain('3 tickers');
  });
});
