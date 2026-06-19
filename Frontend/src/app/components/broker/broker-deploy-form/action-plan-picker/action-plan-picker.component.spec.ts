import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ActionPlan } from '../../../../api/action-plan.types';
import {
  ActionPlanPreviewService,
  type ActionPlanPreviewResponse,
} from '../../../../api/action-plan-preview.service';
import type {
  OptionContractMatch,
  SymbolMatch,
} from '../../../../api/broker-models';
import { BrokerService } from '../../../../services/broker.service';
import { ActionPlanPickerComponent } from './action-plan-picker.component';

const SPY: SymbolMatch = {
  symbol: 'SPY',
  name: 'SPDR S&P 500 ETF Trust',
  exchange: 'ARCA',
  currency: 'USD',
  sec_type: 'STK',
  derivative_sec_types: ['OPT'],
};

const QUALIFIED_CALL: OptionContractMatch = {
  con_id: 42,
  symbol: 'SPY',
  local_symbol: 'SPY   251219C00650000',
  trading_class: 'SPY',
  exchange: 'SMART',
  currency: 'USD',
  expiry_ms: 1_766_188_800_000,
  strike: 650.0,
  right: 'C',
  multiplier: 100,
};

function setup(opts: {
  initial?: ActionPlan;
  prefillUnderlying?: string | null;
  previewResponse?: ActionPlanPreviewResponse;
} = {}): {
  fixture: ComponentFixture<ActionPlanPickerComponent>;
  component: ActionPlanPickerComponent;
  preview: { preview: ReturnType<typeof vi.fn> };
  el: HTMLElement;
} {
  TestBed.resetTestingModule();
  const preview = {
    preview: vi.fn().mockResolvedValue(opts.previewResponse ?? { warnings: [] }),
  };
  // Stub BrokerService — the picker injects it indirectly via the
  // shared instrument-card and option-leg-picker components even when
  // the broker UI is not on screen.
  const broker = {
    searchSymbols: vi.fn().mockResolvedValue({ matches: [] }),
    expirations: vi.fn().mockResolvedValue({ symbol: 'SPY', expirations_ms: [] }),
    strikes: vi.fn().mockResolvedValue({
      symbol: 'SPY',
      expiry_ms: 0,
      strikes: [],
      fetched_at_ms: 0,
    }),
    searchOptionContracts: vi.fn().mockResolvedValue({ matches: [QUALIFIED_CALL] }),
  };
  TestBed.configureTestingModule({
    providers: [
      provideZonelessChangeDetection(),
      { provide: ActionPlanPreviewService, useValue: preview },
      { provide: BrokerService, useValue: broker },
    ],
  });
  const fixture = TestBed.createComponent(ActionPlanPickerComponent);
  fixture.componentRef.setInput('actionPlan', opts.initial ?? { on_enter: [], on_exit: [] });
  fixture.componentRef.setInput('prefillUnderlying', opts.prefillUnderlying ?? null);
  fixture.detectChanges();
  return {
    fixture,
    component: fixture.componentInstance,
    preview,
    el: fixture.nativeElement as HTMLElement,
  };
}

async function flushPreview(fixture: ComponentFixture<ActionPlanPickerComponent>) {
  // Debounce window is 150ms; advance fake timers then flush microtasks.
  await vi.advanceTimersByTimeAsync(200);
  fixture.detectChanges();
  await Promise.resolve();
  fixture.detectChanges();
}

afterEach(() => TestBed.resetTestingModule());

function queryButton(root: HTMLElement, selector: string): HTMLButtonElement {
  const el = root.querySelector(selector);
  if (!(el instanceof HTMLButtonElement)) {
    throw new Error(`expected a HTMLButtonElement for ${selector}, got ${el}`);
  }
  return el;
}

describe('ActionPlanPickerComponent — Slice 1B/1F', () => {
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

  // Slice 1F (#605) — broker-driven picker flow.

  it('clicking "+ Add stock" opens the broker symbol picker', () => {
    const { fixture, el } = setup();

    queryButton(el, '[data-testid="action-plan-picker-enter-add"]').click();
    fixture.detectChanges();

    expect(el.querySelector('[data-testid="action-plan-picker-stock-symbol"]')).not.toBeNull();
    expect(fixture.componentInstance.pickerState()).toEqual({ mode: 'symbol', intent: 'stock' });
    // No leg should be created yet — the picker is awaiting symbol selection.
    expect(fixture.componentInstance.actionPlan().on_enter).toEqual([]);
  });

  it('picking a stock symbol appends a stock leg with the broker-derived underlying', () => {
    const { fixture, component } = setup();

    component.beginAddStock();
    component.onSymbolPicked(SPY);
    fixture.detectChanges();

    const plan = fixture.componentInstance.actionPlan();
    expect(plan.on_enter.length).toBe(1);
    expect(plan.on_enter[0].instrument).toEqual({ kind: 'stock', underlying: 'SPY' });
    expect(plan.on_exit.length).toBe(1);
    expect(plan.on_exit[0]).toMatchObject({ kind: 'close_leg', entry_leg_id: plan.on_enter[0].leg_id });
    expect(component.pickerState().mode).toBe('idle');
  });

  it('qualifying an option leg appends one with absolute selectors from the broker', () => {
    const { fixture, component } = setup();

    component.beginAddOption();
    component.onSymbolPicked(SPY);
    component.onOptionLegQualified(QUALIFIED_CALL);
    fixture.detectChanges();

    const plan = fixture.componentInstance.actionPlan();
    expect(plan.on_enter.length).toBe(1);
    const leg = plan.on_enter[0];
    expect(leg.instrument).toEqual({ kind: 'option', underlying: 'SPY' });
    expect(leg).toMatchObject({
      position: 'long',
      qty_ratio: 1,
      right: 'call',
      strike: { selector: 'absolute', strike: 650 },
      expiry: { selector: 'absolute', expiration_ms: 1_766_188_800_000 },
    });
    expect(component.pickerState().mode).toBe('idle');
  });

  it('cancelPicker closes the picker without creating a leg', () => {
    const { fixture, component, el } = setup();

    component.beginAddOption();
    fixture.detectChanges();
    expect(el.querySelector('[data-testid="action-plan-picker-option-symbol"]')).not.toBeNull();

    component.cancelPicker();
    fixture.detectChanges();

    expect(el.querySelector('[data-testid="action-plan-picker-option-symbol"]')).toBeNull();
    expect(fixture.componentInstance.actionPlan().on_enter).toEqual([]);
  });

  it('removing an entry leg cascades the removal of its mirrored close_leg', () => {
    const { fixture, component, el } = setup();

    component.beginAddStock();
    component.onSymbolPicked(SPY);
    fixture.detectChanges();
    const legId = fixture.componentInstance.actionPlan().on_enter[0].leg_id;

    queryButton(el, `[data-testid="action-plan-picker-enter-remove-${legId}"]`).click();
    fixture.detectChanges();

    const plan = fixture.componentInstance.actionPlan();
    expect(plan.on_enter).toEqual([]);
    expect(plan.on_exit).toEqual([]);
  });

  it('reveals option-specific picker rows only when the leg is an option', () => {
    const { fixture, component, el } = setup();

    component.beginAddStock();
    component.onSymbolPicked(SPY);
    component.beginAddOption();
    component.onSymbolPicked(SPY);
    component.onOptionLegQualified(QUALIFIED_CALL);
    fixture.detectChanges();

    const plan = fixture.componentInstance.actionPlan();
    const stockLegId = plan.on_enter[0].leg_id;
    const optionLegId = plan.on_enter[1].leg_id;

    expect(
      el.querySelector(`[data-testid="action-plan-picker-option-fields-${optionLegId}"]`),
    ).not.toBeNull();
    expect(
      el.querySelector(`[data-testid="action-plan-picker-option-fields-${stockLegId}"]`),
    ).toBeNull();
  });

  // Slice 1D (#597) — debounced preview-endpoint call + inline warnings.

  it('debounces calls to the preview endpoint on plan change', async () => {
    vi.useFakeTimers();
    try {
      const { fixture, component, preview } = setup();
      preview.preview.mockClear();

      component.beginAddStock();
      component.onSymbolPicked(SPY);
      fixture.detectChanges();
      component.beginAddStock();
      component.onSymbolPicked(SPY);
      fixture.detectChanges();
      // Two rapid changes — preview should only fire after the debounce window settles.
      expect(preview.preview).not.toHaveBeenCalled();

      await flushPreview(fixture);

      expect(preview.preview).toHaveBeenCalledTimes(1);
      const sentPlan = preview.preview.mock.calls[0][0];
      expect(sentPlan.on_enter.length).toBe(2);
    } finally {
      vi.useRealTimers();
    }
  });

  it('renders warning rows from the preview response', async () => {
    vi.useFakeTimers();
    try {
      const { fixture, component, el } = setup({
        previewResponse: {
          warnings: [
            {
              code: 'orphan_entry',
              message: "Entry leg 'leg_1' has no matching close_leg.",
              leg_id: 'leg_1',
            },
          ],
        },
      });

      component.beginAddStock();
      component.onSymbolPicked(SPY);
      fixture.detectChanges();
      await flushPreview(fixture);

      const warnings = el.querySelector<HTMLElement>(
        '[data-testid="action-plan-picker-warnings"]',
      );
      expect(warnings).not.toBeNull();
      expect(warnings?.textContent ?? '').toContain('orphan_entry');
      expect(warnings?.textContent ?? '').toContain('leg_1');
    } finally {
      vi.useRealTimers();
    }
  });

  it('removing a close_leg leaves its entry leg in place', () => {
    const { fixture, component, el } = setup();

    component.beginAddStock();
    component.onSymbolPicked(SPY);
    fixture.detectChanges();
    const legId = fixture.componentInstance.actionPlan().on_enter[0].leg_id;

    queryButton(el, `[data-testid="action-plan-picker-exit-remove-${legId}"]`).click();
    fixture.detectChanges();

    const plan = fixture.componentInstance.actionPlan();
    expect(plan.on_enter.length).toBe(1);
    expect(plan.on_exit).toEqual([]);
  });

});
