/**
 * TickerRangePicker — pure-function tests + component-level tests.
 *
 * The pure-function tests cover ``computeAdvisories``.
 *
 * The component-level tests verify picker presentation and flags
 * (``presentation`` removes duplicate host chrome, ``availableMultipliers``
 * passes through to SamplingCard, ``hideSampling`` collapses Sampling) and
 * that the dates take the bound the host states.
 */
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach } from 'vitest';

import { heldBounds } from '../date-picker';
import { fakePickerWorld } from '../symbol-picker/testing/fake-picker-world';
import { TickerRangePickerComponent } from './ticker-range-picker.component';
import { computeAdvisories, type TickerRange } from './ticker-range-picker.types';

describe('computeAdvisories', () => {
  it('suggests hour bars when minute range > 90 days', () => {
    const advisories = computeAdvisories({ symbol: 'SPY', from: '2025-01-01', to: '2026-04-01', resolution: 'minute' });
    expect(advisories.some((a) => a.kind === 'suggest' && a.action?.patch.resolution === 'hour')).toBe(true);
  });

  it('bad-marks a minute range > 365 days', () => {
    const advisories = computeAdvisories({ symbol: 'SPY', from: '2024-01-01', to: '2026-04-01', resolution: 'minute' });
    expect(advisories.some((a) => a.kind === 'bad')).toBe(true);
  });

  it('emits no advisories in the happy path', () => {
    expect(computeAdvisories({ symbol: 'SPY', from: '2026-04-01', to: '2026-04-23', resolution: 'daily', autoFetch: false })).toEqual([]);
  });

  it('emits none while a date field holds no usable date', () => {
    expect(computeAdvisories({ symbol: 'SPY', from: '', to: '2026-04-01', resolution: 'minute' })).toEqual([]);
  });
});

describe('TickerRangePickerComponent (flags)', () => {
  const baseValue: TickerRange = {
    symbol: 'SPY',
    from: '2025-04-01',
    to: '2025-04-30',
    resolution: 'minute',
  };

  let fixture: ComponentFixture<TickerRangePickerComponent>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    await TestBed.configureTestingModule({
      imports: [TickerRangePickerComponent],
      providers: fakePickerWorld().providers,
    }).compileComponents();

    fixture = TestBed.createComponent(TickerRangePickerComponent);
    fixture.componentRef.setInput('value', baseValue);
    fixture.componentRef.setInput('dateBounds', 'provider');
  });

  it('renders the Sampling card by default', () => {
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-sampling-card')).not.toBeNull();
  });

  it('keeps the standalone Backtest data frame by default', () => {
    fixture.detectChanges();

    expect(fixture.nativeElement.querySelector('.picker-v2__head')).not.toBeNull();
    expect(fixture.nativeElement.textContent).toContain('Backtest data');
  });

  it('removes the standalone frame when embedded in a host section', () => {
    fixture.componentRef.setInput('presentation', 'embedded');
    fixture.detectChanges();

    const picker = fixture.nativeElement.querySelector('.picker-v2');
    expect(picker?.classList.contains('picker-v2--embedded')).toBe(true);
    expect(fixture.nativeElement.querySelector('.picker-v2__head')).toBeNull();
    expect(fixture.nativeElement.querySelector('app-instrument-card')).not.toBeNull();
    expect(fixture.nativeElement.querySelector('app-time-window-card')).not.toBeNull();
    expect(fixture.nativeElement.querySelector('app-sampling-card')).not.toBeNull();
  });

  it('hideSampling=true collapses the Sampling card', () => {
    fixture.componentRef.setInput('hideSampling', true);
    fixture.detectChanges();
    expect(fixture.nativeElement.querySelector('app-sampling-card')).toBeNull();
  });

  it('passes availableMultipliers through to the Sampling card', () => {
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

  const fiveYears = (): string | null => {
    const button = [...fixture.nativeElement.querySelectorAll('.date-range__range')].find((el: HTMLElement) => el.textContent?.trim() === '5Y');
    return button instanceof HTMLElement ? button.getAttribute('aria-label') : null;
  };

  it("bounds the dates by the provider's history when the host's run fetches its own bars", async () => {
    await fixture.whenStable();

    expect(fiveYears()).toBe('5Y, unavailable: The provider serves bars from 2021-09-21.');
  });

  it("takes a host's own bound, and any day when the host's dates need not be sessions", async () => {
    fixture.componentRef.setInput('dateBounds', heldBounds('SPY', '2024-01-02', '2026-09-18'));
    fixture.componentRef.setInput('sessionsOnly', false);
    await fixture.whenStable();
    expect(fiveYears()).toBe('5Y, unavailable: SPY is held from 2024-01-02.');

    const start: HTMLInputElement = fixture.nativeElement.querySelector("input[aria-label='Start date']");
    start.value = '2025-04-05';
    start.dispatchEvent(new Event('change'));
    await fixture.whenStable();

    // A Saturday.
    expect(fixture.componentInstance.value().from).toBe('2025-04-05');
  });

  it('keeps a typed date when a sampling control is tapped before the page re-renders', async () => {
    await fixture.whenStable();
    const start: HTMLInputElement = fixture.nativeElement.querySelector("input[aria-label='Start date']");
    const daily = [...fixture.nativeElement.querySelectorAll('.res-btn')].find((el: HTMLElement) => el.textContent?.trim() === '1d');

    // One task, as a touch tap delivers them: the field's change, then the click.
    start.value = '2025-04-07';
    start.dispatchEvent(new Event('change'));
    daily.click();

    expect(fixture.componentInstance.value()).toMatchObject({ from: '2025-04-07', resolution: 'daily' });
  });
});
