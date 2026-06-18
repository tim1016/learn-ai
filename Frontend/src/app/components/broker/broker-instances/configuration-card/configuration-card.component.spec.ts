import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, describe, expect, it } from 'vitest';
import type { InstanceSizing, InstanceStartDefaults } from '../../../../api/live-instances.types';

import { ConfigurationCardComponent } from './configuration-card.component';

function render(opts: {
  startDefaults?: InstanceStartDefaults | null;
  sizing?: InstanceSizing | null;
  canRedeploy?: boolean;
  redeployQueryParams?: Record<string, string>;
}): HTMLElement {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection(), provideRouter([])],
  });
  const fixture = TestBed.createComponent(ConfigurationCardComponent);
  fixture.componentRef.setInput('startDefaults', opts.startDefaults ?? null);
  fixture.componentRef.setInput('sizing', opts.sizing ?? null);
  fixture.componentRef.setInput('canRedeploy', opts.canRedeploy ?? true);
  fixture.componentRef.setInput(
    'redeployQueryParams',
    opts.redeployQueryParams ?? { strategy_key: 'spy_breakout_15m' },
  );
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
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
    const el = render({ startDefaults: SPY_DEFAULTS });

    expect(el.textContent ?? '').toContain('spy_breakout_15m');
  });

  it('renders the resolved sizing preset', () => {
    const el = render({
      startDefaults: SPY_DEFAULTS,
      sizing: SAFE_CANARY_SIZING,
    });

    expect(
      el.querySelector('[data-testid="configuration-sizing"]')?.textContent?.trim(),
    ).toBe('Safe canary');
  });

  it('renders an Edit link to /broker/deploy with redeploy query params when canRedeploy', () => {
    const el = render({
      startDefaults: SPY_DEFAULTS,
      canRedeploy: true,
      redeployQueryParams: { strategy_key: 'spy_breakout_15m', instance_id: 'spy_15m' },
    });

    const link = el.querySelector<HTMLAnchorElement>('a[data-testid="configuration-edit"]');
    expect(link).not.toBeNull();
    expect(link?.getAttribute('href')).toContain('/broker/deploy');
    expect(link?.getAttribute('href')).toContain('strategy_key=spy_breakout_15m');
    expect(link?.getAttribute('href')).toContain('instance_id=spy_15m');
  });

  it('renders an "Edit available after stop" note when redeploy is blocked', () => {
    const el = render({ startDefaults: SPY_DEFAULTS, canRedeploy: false });

    expect(el.querySelector('a[data-testid="configuration-edit"]')).toBeNull();
    expect(
      el.querySelector('[data-testid="configuration-edit-disabled"]'),
    ).not.toBeNull();
  });

  it('renders the empty-state CTA when no strategy is configured', () => {
    const el = render({ startDefaults: null, canRedeploy: false });

    expect(el.querySelector('[data-testid="configuration-empty"]')).not.toBeNull();
    expect(el.textContent ?? '').toContain('Configure to trade');
  });

  it('routes the empty-state CTA to /broker/deploy', () => {
    const el = render({ startDefaults: null, canRedeploy: false });

    const link = el.querySelector<HTMLAnchorElement>('a[data-testid="configuration-cta"]');
    expect(link?.getAttribute('href')).toContain('/broker/deploy');
  });
});
