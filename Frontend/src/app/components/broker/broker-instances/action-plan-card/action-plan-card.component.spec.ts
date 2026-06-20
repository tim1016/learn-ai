import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, describe, expect, it } from 'vitest';
import type { ActionPlan } from '../../../../api/action-plan.types';
import type { OperatorSurfaceActionPlan } from '../../../../api/live-instances.types';
import { ActionPlanCardComponent } from './action-plan-card.component';

const READY_PROJECTION: OperatorSurfaceActionPlan = {
  consumption: 'DECLARATIVE_ONLY',
  anomaly_verdict: 'READY',
};

function render(
  actionPlan: ActionPlan | null,
  projection: OperatorSurfaceActionPlan = READY_PROJECTION,
): HTMLElement {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(ActionPlanCardComponent);
  fixture.componentRef.setInput('actionPlan', actionPlan);
  fixture.componentRef.setInput('projection', projection);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('ActionPlanCardComponent', () => {
  // PRD #607 / Slice 5 (#612) — the previously hardcoded
  // "not active until Slice 4" string is removed; the one-line
  // summary's consumption phrasing replaces it.
  it('renders the consumption-driven one-line summary for an empty plan', () => {
    const el = render({ on_enter: [], on_exit: [] });

    const card = el.querySelector<HTMLElement>('[data-testid="action-plan-card"]');
    expect(card).not.toBeNull();
    expect(card?.textContent ?? '').toContain('declarative only');
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

  // Slice 1E (#598) — "Redeploy with changes" CTA + deep link.

  it('does not render the redeploy CTA when no parent run is known', () => {
    const el = render({ on_enter: [], on_exit: [] });

    expect(
      el.querySelector('[data-testid="action-plan-card-redeploy-cta"]'),
    ).toBeNull();
  });

  it('renders the redeploy CTA with parent_run_id in the deploy-form deep link when provided', () => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideZonelessChangeDetection(), provideRouter([])],
    });
    const fixture = TestBed.createComponent(ActionPlanCardComponent);
    fixture.componentRef.setInput('actionPlan', { on_enter: [], on_exit: [] });
    fixture.componentRef.setInput('parentRunId', 'run-abc12345');
    fixture.detectChanges();
    const el = fixture.nativeElement as HTMLElement;

    const cta = el.querySelector<HTMLAnchorElement>(
      '[data-testid="action-plan-card-redeploy-cta"]',
    );
    expect(cta).not.toBeNull();
    expect(cta?.getAttribute('href')).toContain('parent_run_id=run-abc12345');
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

  // PRD #607 / Slice 5 (#612) — consumption-driven phrasing + collapse.

  it.each([
    ['ACTIVE', 'engine-active'],
    ['DECLARATIVE_ONLY', 'declarative only'],
    ['UNKNOWN', 'activation unknown'],
  ] as const)(
    'phrases the one-line summary using consumption=%s',
    (consumption, phrase) => {
      const el = render({ on_enter: [], on_exit: [] }, {
        consumption,
        anomaly_verdict: 'READY',
      });
      expect(
        el
          .querySelector('[data-testid="action-plan-one-line-summary"]')
          ?.textContent?.toLowerCase(),
      ).toContain(phrase);
    },
  );

  it('counts entry and exit legs in the one-line summary', () => {
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
    expect(
      el
        .querySelector('[data-testid="action-plan-one-line-summary"]')
        ?.textContent ?? '',
    ).toContain('1 enter · 1 exit');
  });

  it('collapses on READY verdict and expands on attention verdicts', () => {
    const ready = render({ on_enter: [], on_exit: [] }, {
      consumption: 'DECLARATIVE_ONLY',
      anomaly_verdict: 'READY',
    });
    expect(ready.getAttribute('data-collapsed')).toBe('true');

    const attention = render({ on_enter: [], on_exit: [] }, {
      consumption: 'UNKNOWN',
      anomaly_verdict: 'UNKNOWN',
    });
    expect(attention.getAttribute('data-collapsed')).toBe('false');
  });

  it('renders no toggle on attention verdicts (Option A)', () => {
    const el = render({ on_enter: [], on_exit: [] }, {
      consumption: 'UNKNOWN',
      anomaly_verdict: 'ATTENTION',
    });
    expect(el.querySelector('[data-testid="action-plan-toggle"]')).toBeNull();
  });

  it('renders a toggle on READY cards that the operator can use to manually expand', () => {
    const el = render({ on_enter: [], on_exit: [] }, {
      consumption: 'DECLARATIVE_ONLY',
      anomaly_verdict: 'READY',
    });
    const toggle = el.querySelector<HTMLButtonElement>('[data-testid="action-plan-toggle"]');
    expect(toggle).not.toBeNull();
    expect(toggle?.getAttribute('aria-expanded')).toBe('false');
  });
});
