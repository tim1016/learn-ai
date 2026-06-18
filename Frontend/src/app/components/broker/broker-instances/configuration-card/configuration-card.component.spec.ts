import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type { InstanceSizing, InstanceStartDefaults } from '../../../../api/live-instances.types';

import { ConfigurationCardComponent } from './configuration-card.component';

function render(opts: {
  startDefaults?: InstanceStartDefaults | null;
  sizing?: InstanceSizing | null;
}): { el: HTMLElement; component: ConfigurationCardComponent } {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection()],
  });
  const fixture = TestBed.createComponent(ConfigurationCardComponent);
  fixture.componentRef.setInput('startDefaults', opts.startDefaults ?? null);
  fixture.componentRef.setInput('sizing', opts.sizing ?? null);
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    component: fixture.componentInstance,
  };
}

afterEach(() => TestBed.resetTestingModule());

const SPY_DEFAULTS: InstanceStartDefaults = {
  strategy: 'spy_breakout_15m',
  readonly: false,
  hydrate_policy: 'optional',
  max_orders_per_day: 5,
  ibkr_host: '',
};

const SAFE_CANARY_SIZING: InstanceSizing = {
  policy: { kind: 'fraction', fraction: 0.01 },
  preset: 'safe_canary',
} as unknown as InstanceSizing;

describe('ConfigurationCardComponent', () => {
  it('renders the configured strategy name', () => {
    const { el } = render({ startDefaults: SPY_DEFAULTS });

    expect(el.textContent ?? '').toContain('spy_breakout_15m');
  });

  it('renders the resolved sizing preset', () => {
    const { el } = render({
      startDefaults: SPY_DEFAULTS,
      sizing: SAFE_CANARY_SIZING,
    });

    expect(
      el.querySelector('[data-testid="configuration-sizing"]')?.textContent?.trim(),
    ).toBe('Safe canary');
  });

  it('emits editRequested when the operator clicks Edit', () => {
    const { el, component } = render({ startDefaults: SPY_DEFAULTS });
    let fired = 0;
    component.editRequested.subscribe(() => (fired += 1));

    el.querySelector<HTMLButtonElement>('[data-testid="configuration-edit"]')?.click();

    expect(fired).toBe(1);
  });

  it('renders the empty-state CTA when no strategy is configured', () => {
    const { el } = render({ startDefaults: null });

    expect(el.querySelector('[data-testid="configuration-empty"]')).not.toBeNull();
    expect(el.textContent ?? '').toContain('Configure to trade');
  });

  it('emits editRequested when the empty-state CTA is clicked', () => {
    const { el, component } = render({ startDefaults: null });
    let fired = 0;
    component.editRequested.subscribe(() => (fired += 1));

    el.querySelector<HTMLButtonElement>('[data-testid="configuration-edit"]')?.click();

    expect(fired).toBe(1);
  });
});
