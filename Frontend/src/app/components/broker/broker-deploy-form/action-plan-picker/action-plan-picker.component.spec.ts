import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed, ComponentFixture } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type { ActionPlan } from '../../../../api/action-plan.types';
import { ActionPlanPickerComponent } from './action-plan-picker.component';

function setup(opts: {
  initial?: ActionPlan;
  prefillUnderlying?: string | null;
} = {}): {
  fixture: ComponentFixture<ActionPlanPickerComponent>;
  component: ActionPlanPickerComponent;
  el: HTMLElement;
} {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(ActionPlanPickerComponent);
  fixture.componentRef.setInput('actionPlan', opts.initial ?? { on_enter: [], on_exit: [] });
  fixture.componentRef.setInput('prefillUnderlying', opts.prefillUnderlying ?? null);
  fixture.detectChanges();
  return { fixture, component: fixture.componentInstance, el: fixture.nativeElement as HTMLElement };
}

afterEach(() => TestBed.resetTestingModule());

function queryButton(root: HTMLElement, selector: string): HTMLButtonElement {
  const el = root.querySelector(selector);
  if (!(el instanceof HTMLButtonElement)) {
    throw new Error(`expected a HTMLButtonElement for ${selector}, got ${el}`);
  }
  return el;
}

describe('ActionPlanPickerComponent — Slice 1B', () => {
  it('renders ON ENTER and ON EXIT sections, each with [+ Add]', () => {
    const { el } = setup();

    const enterSection = el.querySelector<HTMLElement>('[data-testid="action-plan-picker-enter"]');
    const exitSection = el.querySelector<HTMLElement>('[data-testid="action-plan-picker-exit"]');
    expect(enterSection).not.toBeNull();
    expect(exitSection).not.toBeNull();
    expect(enterSection?.textContent ?? '').toContain('ON ENTER');
    expect(exitSection?.textContent ?? '').toContain('ON EXIT');
    expect(el.querySelector('[data-testid="action-plan-picker-enter-add"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="action-plan-picker-exit-add"]')).not.toBeNull();
  });

  it('adding a stock entry leg auto-fills a mirrored close_leg in on_exit', () => {
    const { fixture, el } = setup({ prefillUnderlying: 'SPY' });

    queryButton(el, '[data-testid="action-plan-picker-enter-add"]').click();
    fixture.detectChanges();

    const plan = fixture.componentInstance.actionPlan();
    expect(plan.on_enter.length).toBe(1);
    expect(plan.on_exit.length).toBe(1);
    expect(plan.on_enter[0].instrument.underlying).toBe('SPY');
    expect(plan.on_exit[0]).toMatchObject({
      kind: 'close_leg',
      entry_leg_id: plan.on_enter[0].leg_id,
    });
  });

  it('the submitted payload carries the literal underlying even when prefill was used', () => {
    const { fixture, el } = setup({ prefillUnderlying: 'SPY' });

    queryButton(el, '[data-testid="action-plan-picker-enter-add"]').click();
    fixture.detectChanges();

    // Re-render with a different prefill — the already-added leg must
    // keep "SPY" literally (no implicit context-dependence).
    fixture.componentRef.setInput('prefillUnderlying', 'QQQ');
    fixture.detectChanges();

    const plan = fixture.componentInstance.actionPlan();
    expect(plan.on_enter[0].instrument.underlying).toBe('SPY');
  });

  it('removing an entry leg cascades the removal of its mirrored close_leg', () => {
    const { fixture, el } = setup({ prefillUnderlying: 'SPY' });

    queryButton(el, '[data-testid="action-plan-picker-enter-add"]').click();
    fixture.detectChanges();
    const legId = fixture.componentInstance.actionPlan().on_enter[0].leg_id;

    queryButton(el, `[data-testid="action-plan-picker-enter-remove-${legId}"]`).click();
    fixture.detectChanges();

    const plan = fixture.componentInstance.actionPlan();
    expect(plan.on_enter).toEqual([]);
    expect(plan.on_exit).toEqual([]);
  });

  it('removing a close_leg leaves its entry leg in place', () => {
    const { fixture, el } = setup({ prefillUnderlying: 'SPY' });

    queryButton(el, '[data-testid="action-plan-picker-enter-add"]').click();
    fixture.detectChanges();
    const legId = fixture.componentInstance.actionPlan().on_enter[0].leg_id;

    queryButton(el, `[data-testid="action-plan-picker-exit-remove-${legId}"]`).click();
    fixture.detectChanges();

    const plan = fixture.componentInstance.actionPlan();
    expect(plan.on_enter.length).toBe(1);
    expect(plan.on_exit).toEqual([]);
  });
});
