import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { SamplingCardComponent, type SamplingChange } from './sampling-card.component';
import type { TickerRange } from '../ticker-range-picker.types';

describe('SamplingCardComponent', () => {
  const baseValue: TickerRange = {
    symbol: 'SPY',
    from: '2025-04-01',
    to: '2025-04-30',
    resolution: 'minute',
  };

  let fixture: ComponentFixture<SamplingCardComponent>;
  let component: SamplingCardComponent;
  let changes: SamplingChange[];

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [SamplingCardComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(SamplingCardComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('value', baseValue);
    changes = [];
    component.changed.subscribe((change) => changes.push(change));
  });

  it('renders the three resolution toggles by default', () => {
    fixture.detectChanges();
    const buttons = fixture.nativeElement.querySelectorAll('.res-btn');
    expect(buttons.length).toBe(3);
  });

  it('does NOT render multiplier dropdown when availableMultipliers is empty', () => {
    fixture.detectChanges();
    const select = fixture.nativeElement.querySelector('.multiplier__select');
    expect(select).toBeNull();
  });

  it('renders multiplier dropdown when availableMultipliers is non-empty', () => {
    fixture.componentRef.setInput('availableMultipliers', [1, 5, 15]);
    fixture.detectChanges();
    const select: HTMLSelectElement | null = fixture.nativeElement.querySelector(
      '.multiplier__select',
    );
    expect(select).not.toBeNull();
    if (select) {
      expect(select.querySelectorAll('option').length).toBe(3);
    }
  });

  it('hands its host only the field a control set, never the range it was given', () => {
    fixture.componentRef.setInput('availableMultipliers', [1, 5, 15]);
    fixture.detectChanges();
    component.setMultiplier(15);
    expect(changes).toEqual([{ multiplier: 15 }]);
  });

  it('effectiveMultiplier defaults to 1 when undefined on the value', () => {
    fixture.detectChanges();
    expect(component.effectiveMultiplier()).toBe(1);
  });

  it('setSession to extended is ignored when sessionMode=disabled', () => {
    fixture.componentRef.setInput('sessionMode', 'disabled');
    fixture.detectChanges();
    component.setSession('extended');
    expect(changes).toEqual([]);
  });

  it('hides the entire session group when sessionMode=hidden', () => {
    fixture.componentRef.setInput('sessionMode', 'hidden');
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.session')).toBeNull();
  });
});
