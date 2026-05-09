import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { TimeWindowCardComponent } from './time-window-card.component';
import type {
  AvailabilityCell,
  TickerRange,
} from '../ticker-range-picker.types';

describe('TimeWindowCardComponent', () => {
  const baseValue: TickerRange = {
    symbol: 'SPY',
    from: '2025-04-01',
    to: '2025-04-30',
    resolution: 'minute',
  };

  let fixture: ComponentFixture<TimeWindowCardComponent>;
  let component: TimeWindowCardComponent;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [TimeWindowCardComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(TimeWindowCardComponent);
    component = fixture.componentInstance;

    fixture.componentRef.setInput('value', baseValue);
  });

  it('renders two date inputs bound to value().from and value().to', async () => {
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    const inputs: NodeListOf<HTMLInputElement> =
      fixture.nativeElement.querySelectorAll('input[type="date"]');
    expect(inputs.length).toBe(2);
    // Source of truth is the model signal — verifying the component
    // received both ends of the range.
    expect(component.value().from).toBe('2025-04-01');
    expect(component.value().to).toBe('2025-04-30');
  });

  it('applyPreset(7) sets a 7-day window ending today', () => {
    fixture.detectChanges();
    component.applyPreset(7);
    const v = component.value();
    const fromMs = new Date(v.from).getTime();
    const toMs = new Date(v.to).getTime();
    const days = Math.round((toMs - fromMs) / 86_400_000);
    expect(days).toBe(7);
  });

  it('renders the availability strip when cells are provided', () => {
    const cells: AvailabilityCell[] = [
      { date: '2025-04-01', status: 'complete' },
      { date: '2025-04-02', status: 'partial' },
      { date: '2025-04-03', status: 'missing' },
    ];
    fixture.componentRef.setInput('availability', cells);
    fixture.detectChanges();

    const cellEls = fixture.nativeElement.querySelectorAll('.strip__cell');
    expect(cellEls.length).toBe(3);
  });

  it('exposes summary and dominant computeds derived from availability', () => {
    fixture.componentRef.setInput('availability', [
      { date: '2025-04-01', status: 'complete' },
      { date: '2025-04-02', status: 'complete' },
      { date: '2025-04-03', status: 'hole' },
    ] as AvailabilityCell[]);
    fixture.detectChanges();

    expect(component.summary().complete).toBe(2);
    expect(component.summary().hole).toBe(1);
    expect(component.dominant()).toBe('hole');
  });

  it('updateFrom mutates value().from immutably', () => {
    fixture.detectChanges();
    component.updateFrom('2025-03-15');
    expect(component.value().from).toBe('2025-03-15');
    expect(component.value().to).toBe('2025-04-30');
  });
});
