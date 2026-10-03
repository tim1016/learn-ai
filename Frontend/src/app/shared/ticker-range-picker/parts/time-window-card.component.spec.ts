import { ComponentFixture, TestBed } from '@angular/core/testing';
import { beforeEach, describe, expect, it } from 'vitest';

import { heldBounds } from '../../date-picker';
import { provideFakeDateWorld } from '../../date-picker/testing/fake-trading-calendar';
import type { TickerRange } from '../ticker-range-picker.types';
import { TimeWindowCardComponent } from './time-window-card.component';

const SPY = heldBounds('SPY', '2024-01-02', '2026-09-18');

describe('TimeWindowCardComponent', () => {
  let fixture: ComponentFixture<TimeWindowCardComponent>;

  const range = (overrides: Partial<TickerRange> = {}): TickerRange => ({ symbol: 'SPY', from: '2026-08-18', to: '2026-09-18', resolution: 'minute', ...overrides });
  const quick = (key: string): HTMLButtonElement => {
    const button = [...fixture.nativeElement.querySelectorAll('.date-range__range')].find((el: HTMLElement) => el.textContent?.trim() === key);
    if (!(button instanceof HTMLButtonElement)) throw new Error(`no quick range ${key}`);
    return button;
  };

  beforeEach(async () => {
    await TestBed.configureTestingModule({ imports: [TimeWindowCardComponent], providers: provideFakeDateWorld() }).compileComponents();
    fixture = TestBed.createComponent(TimeWindowCardComponent);
    fixture.componentRef.setInput('bounds', SPY);
  });

  it("writes a picked range into the value's dates in one change and leaves the rest of the value alone", async () => {
    fixture.componentRef.setInput('value', range());
    await fixture.whenStable();
    const changes: TickerRange[] = [];
    fixture.componentInstance.value.subscribe((value) => changes.push(value));

    quick('3M').click();
    await fixture.whenStable();

    expect(changes).toEqual([{ symbol: 'SPY', from: '2026-06-18', to: '2026-09-18', resolution: 'minute' }]);
  });

  it("gives the picker its host's bounds: a range the data cannot serve is greyed with the reason", async () => {
    fixture.componentRef.setInput('value', range());
    await fixture.whenStable();

    expect(quick('5Y').getAttribute('aria-label')).toBe('5Y, unavailable: SPY is held from 2024-01-02.');
  });
});
