import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';
import { SamplingCardComponent } from './sampling-card.component';
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

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [SamplingCardComponent],
    }).compileComponents();

    fixture = TestBed.createComponent(SamplingCardComponent);
    component = fixture.componentInstance;
    fixture.componentRef.setInput('value', baseValue);
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
    expect(select!.querySelectorAll('option').length).toBe(3);
  });

  it('setMultiplier updates value().multiplier', () => {
    fixture.componentRef.setInput('availableMultipliers', [1, 5, 15]);
    fixture.detectChanges();
    component.setMultiplier(15);
    expect(component.value().multiplier).toBe(15);
  });

  it('effectiveMultiplier defaults to 1 when undefined on the value', () => {
    fixture.detectChanges();
    expect(component.effectiveMultiplier()).toBe(1);
  });

  it('setSession to extended is ignored when sessionMode=disabled', () => {
    fixture.componentRef.setInput('sessionMode', 'disabled');
    fixture.detectChanges();
    component.setSession('extended');
    expect(component.value().session).toBeUndefined();
  });

  it('hides the entire session group when sessionMode=hidden', () => {
    fixture.componentRef.setInput('sessionMode', 'hidden');
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('.session')).toBeNull();
  });
});
