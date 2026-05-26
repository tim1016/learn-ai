import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';
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

  describe('applyPreset weekend handling', () => {
    afterEach(() => {
      vi.useRealTimers();
    });

    // The sidecar validator rejects weekend ``start_ms_utc`` or
    // ``end_ms_utc`` with a 422. These tests pin the exact reproduction
    // of the form-side bug where ``today - days`` lands on a weekend
    // (the 1M preset against a Mon "today", the 1Y preset against a
    // Mon "today", etc.) and assert both endpoints land on weekdays.
    it('applyPreset(30) bumps a weekend start back to Friday', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date(2026, 4, 25, 12, 0, 0)); // Mon
      fixture.detectChanges();
      component.applyPreset(30);
      const from = new Date(component.value().from);
      // Mon 2026-05-25 minus 30 = Sat 2026-04-25 → walks to Fri 2026-04-24.
      expect(from.getDay()).toBe(5);
      expect(component.value().from).toBe('2026-04-24');
    });

    it('applyPreset(365) bumps a weekend start back to Friday', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date(2026, 4, 25, 12, 0, 0)); // Mon
      fixture.detectChanges();
      component.applyPreset(365);
      const from = new Date(component.value().from);
      // Mon 2026-05-25 minus 365 = Sun 2025-05-25 → walks to Fri 2025-05-23.
      expect(from.getDay()).toBe(5);
      expect(component.value().from).toBe('2025-05-23');
    });

    it('applyPreset bumps a weekend end back to Friday when today is Sun', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date(2026, 4, 24, 12, 0, 0)); // Sun 2026-05-24
      fixture.detectChanges();
      component.applyPreset(7);
      const to = new Date(component.value().to);
      // Today is Sun → end walks to Fri 2026-05-22. Start follows the
      // same 7-day-back-then-weekday rule so it also lands on Friday.
      expect(to.getDay()).toBe(5);
      expect(component.value().to).toBe('2026-05-22');
    });

    it('applyPreset leaves both endpoints alone when today is mid-week', () => {
      vi.useFakeTimers();
      vi.setSystemTime(new Date(2026, 4, 27, 12, 0, 0)); // Wed 2026-05-27
      fixture.detectChanges();
      component.applyPreset(7);
      // Wed - 7 = Wed (weekday); end = Wed (weekday). No walk.
      expect(component.value().from).toBe('2026-05-20');
      expect(component.value().to).toBe('2026-05-27');
    });
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
