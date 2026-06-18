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
});
