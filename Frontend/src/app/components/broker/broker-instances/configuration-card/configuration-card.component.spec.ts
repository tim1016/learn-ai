import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, describe, expect, it } from 'vitest';
import type {
  InstanceBrokerView,
  InstanceProvenance,
  InstanceSizing,
  InstanceStartDefaults,
  OperatorSurfaceConfiguration,
  OperatorSurfaceCurrentRisk,
  OperatorSurfaceDailyOrderCap,
} from '../../../../api/live-instances.types';

import { ConfigurationCardComponent } from './configuration-card.component';

interface RenderOptions {
  startDefaults?: InstanceStartDefaults | null;
  sizing?: InstanceSizing | null;
  provenance?: InstanceProvenance | null;
  broker?: InstanceBrokerView | null;
  configuration?: OperatorSurfaceConfiguration;
  currentRisk?: OperatorSurfaceCurrentRisk;
  dailyOrderCap?: OperatorSurfaceDailyOrderCap;
  canRedeploy?: boolean;
  redeployQueryParams?: Record<string, string>;
}

const DEFAULT_CONFIG: OperatorSurfaceConfiguration = {
  verdict: 'READY',
  reason_codes: [],
};

const DEFAULT_RISK: OperatorSurfaceCurrentRisk = {
  posture: 'FLAT',
  pending_order_count: 0,
  verdict: 'READY',
  unrealized_pnl: null,
};

const DEFAULT_CAP: OperatorSurfaceDailyOrderCap = { used: null, limit: null };

function render(opts: RenderOptions): { el: HTMLElement; host: HTMLElement } {
  TestBed.resetTestingModule();
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection(), provideRouter([])],
  });
  const fixture = TestBed.createComponent(ConfigurationCardComponent);
  fixture.componentRef.setInput('startDefaults', opts.startDefaults ?? null);
  fixture.componentRef.setInput('sizing', opts.sizing ?? null);
  fixture.componentRef.setInput('provenance', opts.provenance ?? null);
  fixture.componentRef.setInput('broker', opts.broker ?? null);
  fixture.componentRef.setInput('configuration', opts.configuration ?? DEFAULT_CONFIG);
  fixture.componentRef.setInput('currentRisk', opts.currentRisk ?? DEFAULT_RISK);
  fixture.componentRef.setInput('dailyOrderCap', opts.dailyOrderCap ?? DEFAULT_CAP);
  fixture.componentRef.setInput('canRedeploy', opts.canRedeploy ?? true);
  fixture.componentRef.setInput(
    'redeployQueryParams',
    opts.redeployQueryParams ?? { strategy_key: 'spy_breakout_15m' },
  );
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    host: fixture.nativeElement as HTMLElement,
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
  sizing_provenance: 'reference_native',
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

  it('renders an Edit link to /broker/deploy with redeploy query params when canRedeploy', () => {
    const { el } = render({
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
    const { el } = render({ startDefaults: SPY_DEFAULTS, canRedeploy: false });

    expect(el.querySelector('a[data-testid="configuration-edit"]')).toBeNull();
    expect(
      el.querySelector('[data-testid="configuration-edit-disabled"]'),
    ).not.toBeNull();
  });

  it('renders the empty-state CTA when no strategy is configured', () => {
    const { el } = render({ startDefaults: null, canRedeploy: false });

    expect(el.querySelector('[data-testid="configuration-empty"]')).not.toBeNull();
    expect(el.textContent ?? '').toContain('Configure to trade');
  });

  it('routes the empty-state CTA to /broker/deploy', () => {
    const { el } = render({ startDefaults: null, canRedeploy: false });

    const link = el.querySelector<HTMLAnchorElement>('a[data-testid="configuration-cta"]');
    expect(link?.getAttribute('href')).toContain('/broker/deploy');
  });

  // ─ PRD #607 / Slice 4 (#611) — body rows + risk-chip + collapse ─

  it.each([
    [
      { used: 3, limit: 50 },
      '3',
      '50',
    ],
    [{ used: null, limit: 50 }, '—', '50'],
    [{ used: 0, limit: null }, '0', '—'],
    [{ used: null, limit: null }, '—', '—'],
  ] as const)(
    'renders DAILY CAP from operator_surface.daily_order_cap (used=%o)',
    (cap, expectedUsed, expectedLimit) => {
      const { el } = render({
        startDefaults: SPY_DEFAULTS,
        sizing: SAFE_CANARY_SIZING,
        dailyOrderCap: cap,
      });
      expect(
        el
          .querySelector('[data-testid="row-daily-cap-limit"]')
          ?.textContent?.replace(/\s+/g, ''),
      ).toBe(expectedLimit);
      expect(
        el
          .querySelector('[data-testid="row-daily-cap-used"]')
          ?.textContent?.replace(/\s+/g, ''),
      ).toContain(expectedUsed);
    },
  );

  it.each([
    ['reference_native', 'reference native'],
    ['live_override', 'live override'],
    ['spec_default', 'spec default'],
  ] as const)(
    'renders the SIZING provenance badge text for %s',
    (provenance, expected) => {
      const { el } = render({
        startDefaults: SPY_DEFAULTS,
        sizing: { ...SAFE_CANARY_SIZING, sizing_provenance: provenance } as InstanceSizing,
      });
      expect(
        el
          .querySelector('[data-testid="row-sizing-provenance-badge"]')
          ?.textContent?.toLowerCase(),
      ).toContain(expected);
    },
  );

  it('renders [provenance unknown] when sizing has no provenance', () => {
    const sizing = {
      policy: { kind: 'fraction', fraction: 0.01 },
      preset: 'safe_canary',
      sizing_provenance: null,
    } as unknown as InstanceSizing;
    const { el } = render({ startDefaults: SPY_DEFAULTS, sizing });
    expect(
      el
        .querySelector('[data-testid="row-sizing-provenance-badge"]')
        ?.textContent?.toLowerCase(),
    ).toContain('provenance unknown');
  });

  it('renders the pinned risk-chip from operator_surface.current_risk', () => {
    const { el } = render({
      startDefaults: SPY_DEFAULTS,
      currentRisk: {
        posture: 'LONG',
        pending_order_count: 2,
        verdict: 'ATTENTION',
        unrealized_pnl: -134.56,
      },
    });
    expect(
      el.querySelector('[data-testid="risk-chip-posture"]')?.textContent?.trim(),
    ).toBe('LONG');
    expect(
      el.querySelector('[data-testid="risk-chip-pending"]')?.textContent?.trim(),
    ).toContain('2');
    // unrealized_pnl rendered via DecimalPipe 1.2-2
    expect(
      el.querySelector('[data-testid="risk-chip-upnl"]')?.textContent?.trim(),
    ).toContain('134.56');
  });

  it('renders — for null pending and omits unrealized PnL slot when null', () => {
    const { el } = render({
      startDefaults: SPY_DEFAULTS,
      currentRisk: {
        posture: 'UNKNOWN',
        pending_order_count: null,
        verdict: 'UNKNOWN',
        unrealized_pnl: null,
      },
    });
    expect(
      el.querySelector('[data-testid="risk-chip-pending"]')?.textContent ?? '',
    ).toContain('—');
    expect(el.querySelector('[data-testid="risk-chip-upnl"]')).toBeNull();
  });

  it.each([
    ['READY', 'true', 'ready'],
    ['ATTENTION', 'false', 'degraded'],
    ['UNKNOWN', 'false', 'unknown'],
  ] as const)(
    'sets [data-collapsed]=%s and [data-verdict]=%s for configuration verdict %s',
    (verdict, expectedCollapsed, expectedAttr) => {
      const { host } = render({
        startDefaults: SPY_DEFAULTS,
        configuration: { verdict, reason_codes: [] },
      });
      expect(host.getAttribute('data-collapsed')).toBe(expectedCollapsed);
      expect(host.getAttribute('data-verdict')).toBe(expectedAttr);
    },
  );

  it('renders no collapse/expand toggle on the card (Option A: attention cards never have one)', () => {
    const { el } = render({
      startDefaults: SPY_DEFAULTS,
      configuration: { verdict: 'ATTENTION', reason_codes: ['STRATEGY_KEY_MISSING'] },
    });
    expect(
      el.querySelector('button[aria-expanded], button[aria-label*="ollapse" i], button[aria-label*="xpand" i]'),
    ).toBeNull();
  });

  it('does not render ORDER MODE / ADVANCED / SIZING DETAIL surfaces (regression guard)', () => {
    const { el } = render({
      startDefaults: SPY_DEFAULTS,
      sizing: SAFE_CANARY_SIZING,
      configuration: { verdict: 'ATTENTION', reason_codes: [] },
    });
    const lower = (el.textContent ?? '').toLowerCase();
    expect(lower).not.toContain('order mode');
    expect(lower).not.toContain('▸ advanced');
    expect(lower).not.toContain('▸ sizing detail');
  });
});
