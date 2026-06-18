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
});
