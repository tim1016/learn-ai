import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type { ActionPlan } from '../../../../api/action-plan.types';
import { ActionPlanCardComponent } from './action-plan-card.component';

const NOT_ACTIVE_LABEL =
  'Declared action plan — not active until engine consumption (Slice 4)';

function render(actionPlan: ActionPlan | null): HTMLElement {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(ActionPlanCardComponent);
  fixture.componentRef.setInput('actionPlan', actionPlan);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('ActionPlanCardComponent', () => {
  it('renders the explicit "not active until Slice 4" label for the empty plan', () => {
    const el = render({ on_enter: [], on_exit: [] });

    const card = el.querySelector<HTMLElement>('[data-testid="action-plan-card"]');
    expect(card).not.toBeNull();
    expect(card?.textContent ?? '').toContain(NOT_ACTIVE_LABEL);
  });

  it('renders nothing when the action plan is absent (legacy / pre-Slice-1A ledgers)', () => {
    const el = render(null);

    expect(el.querySelector('[data-testid="action-plan-card"]')).toBeNull();
  });

  it('communicates the empty-plan state to the operator', () => {
    const el = render({ on_enter: [], on_exit: [] });

    const card = el.querySelector<HTMLElement>('[data-testid="action-plan-card"]');
    expect(card?.textContent ?? '').toContain('No legs declared');
  });

  // Slice 1B — stock entry leg + close_leg.

  it('renders a stock entry leg with underlying, position, and qty_ratio', () => {
    const el = render({
      on_enter: [
        {
          leg_id: 'spy_long',
          instrument: { kind: 'stock', underlying: 'SPY' },
          position: 'long',
          qty_ratio: 1,
        },
      ],
      on_exit: [],
    });

    const entry = el.querySelector<HTMLElement>('[data-testid="action-plan-entry-spy_long"]');
    expect(entry).not.toBeNull();
    const text = entry?.textContent ?? '';
    expect(text).toContain('SPY');
    expect(text.toLowerCase()).toContain('long');
    expect(text).toContain('spy_long');
  });

  it('renders a close_leg reference pointing at its entry leg', () => {
    const el = render({
      on_enter: [
        {
          leg_id: 'spy_long',
          instrument: { kind: 'stock', underlying: 'SPY' },
          position: 'long',
          qty_ratio: 1,
        },
      ],
      on_exit: [{ kind: 'close_leg', entry_leg_id: 'spy_long' }],
    });

    const exitRow = el.querySelector<HTMLElement>('[data-testid="action-plan-exit-spy_long"]');
    expect(exitRow).not.toBeNull();
    expect(exitRow?.textContent ?? '').toContain('spy_long');
    expect((exitRow?.textContent ?? '').toLowerCase()).toContain('close');
  });

  // Slice 1C — human-readable option summaries.

  it('renders an option leg as "Long call · ATM · min_dte 14d"', () => {
    const el = render({
      on_enter: [
        {
          leg_id: 'spy_long_call',
          instrument: { kind: 'option', underlying: 'SPY' },
          position: 'long',
          qty_ratio: 1,
          right: 'call',
          strike: { selector: 'atm' },
          expiry: { selector: 'min_dte', days: 14 },
        },
      ],
      on_exit: [],
    });

    const entry = el.querySelector<HTMLElement>(
      '[data-testid="action-plan-entry-spy_long_call"]',
    );
    expect(entry?.textContent ?? '').toContain('Long call · ATM · min_dte 14d');
  });

  it('renders an atm_offset strike as "ATM+5" and a short put leg', () => {
    const el = render({
      on_enter: [
        {
          leg_id: 'short_otm_put',
          instrument: { kind: 'option', underlying: 'SPY' },
          position: 'short',
          qty_ratio: 1,
          right: 'put',
          strike: { selector: 'atm_offset', offset: -5 },
          expiry: { selector: 'nearest_weekly' },
        },
      ],
      on_exit: [],
    });

    const entry = el.querySelector<HTMLElement>(
      '[data-testid="action-plan-entry-short_otm_put"]',
    );
    expect(entry?.textContent ?? '').toContain('Short put · ATM-5 · nearest weekly');
  });

  it('renders an absolute expiry as the New York date (no UTC drift)', () => {
    // 2026-06-25 16:00 ET (option expiry close) = 2026-06-25 20:00 UTC.
    // The UTC date is also 2026-06-25, but using a wall-clock moment
    // where UTC and NY disagree would only verify the date crosses
    // midnight differently — here we just pin the display.
    const el = render({
      on_enter: [
        {
          leg_id: 'spy_abs',
          instrument: { kind: 'option', underlying: 'SPY' },
          position: 'long',
          qty_ratio: 1,
          right: 'call',
          strike: { selector: 'atm' },
          expiry: { selector: 'absolute', expiration_ms: 1_782_417_600_000 },
        },
      ],
      on_exit: [],
    });

    const entry = el.querySelector<HTMLElement>('[data-testid="action-plan-entry-spy_abs"]');
    expect(entry?.textContent ?? '').toContain('2026-06-25');
  });

  it('renders an absolute expiry in the NY tz when UTC has rolled to the next day', () => {
    // Pick a moment that's NY-side 2026-06-25 23:30 EDT = 2026-06-26 03:30 UTC.
    // The UTC date is 2026-06-26 but the NY date is 2026-06-25 — this
    // pins the timestamp-policy conversion at the rendering boundary.
    const el = render({
      on_enter: [
        {
          leg_id: 'spy_late',
          instrument: { kind: 'option', underlying: 'SPY' },
          position: 'long',
          qty_ratio: 1,
          right: 'call',
          strike: { selector: 'atm' },
          expiry: {
            selector: 'absolute',
            expiration_ms: new Date('2026-06-25T23:30:00-04:00').getTime(),
          },
        },
      ],
      on_exit: [],
    });

    const entry = el.querySelector<HTMLElement>('[data-testid="action-plan-entry-spy_late"]');
    expect(entry?.textContent ?? '').toContain('2026-06-25');
  });
});
