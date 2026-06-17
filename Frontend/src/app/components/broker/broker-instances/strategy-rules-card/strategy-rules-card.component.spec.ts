import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { afterEach, describe, expect, it } from 'vitest';
import type {
  InstanceProvenance,
  InstanceSizing,
  InstanceStartDefaults,
} from '../../../../api/live-instances.types';
import { StrategyRulesCardComponent } from './strategy-rules-card.component';

function makeDefaults(
  overrides: Partial<InstanceStartDefaults> = {},
): InstanceStartDefaults {
  return {
    strategy: 'spy_15m_breakout',
    readonly: false,
    hydrate_policy: 'require',
    max_orders_per_day: 3,
    ibkr_host: 'host.containers.internal:7497',
    strategy_spec_path: '/specs/spy_15m_breakout.json',
    qc_audit_copy_path: '/audits/spy.json',
    qc_cloud_backtest_id: 'qc_12345',
    account_id: 'DU1234567',
    ...overrides,
  };
}

function makeProvenance(
  overrides: Partial<InstanceProvenance> = {},
): InstanceProvenance {
  return {
    run_id: 'run_abc',
    schema_version: '1',
    code_sha: 'a'.repeat(40),
    strategy_spec_path: '/specs/spy_15m_breakout.json',
    strategy_spec_sha256: 'b'.repeat(64),
    qc_audit_copy_path: '/audits/spy.json',
    qc_audit_copy_sha256: 'c'.repeat(64),
    qc_cloud_backtest_id: 'qc_12345',
    account_id: 'DU1234567',
    start_date_ms: 0,
    created_at_ms: 0,
    live_config: {},
    ...overrides,
  };
}

function makeSizing(overrides: Partial<InstanceSizing> = {}): InstanceSizing {
  return {
    policy: null,
    preset: 'safe_canary',
    governed_by: 'live_config',
    sizing_provenance: 'reference_native',
    per_trade_audit: [],
    ...overrides,
  };
}

function render(opts: {
  startDefaults: InstanceStartDefaults | null;
  provenance: InstanceProvenance | null;
  sizing: InstanceSizing | null;
  canRedeploy: boolean;
  redeployQueryParams?: Record<string, string>;
}): { el: HTMLElement; component: StrategyRulesCardComponent } {
  TestBed.configureTestingModule({
    providers: [provideZonelessChangeDetection(), provideRouter([])],
  });
  const fixture = TestBed.createComponent(StrategyRulesCardComponent);
  fixture.componentRef.setInput('startDefaults', opts.startDefaults);
  fixture.componentRef.setInput('provenance', opts.provenance);
  fixture.componentRef.setInput('sizing', opts.sizing);
  fixture.componentRef.setInput('instanceId', 'spy_15m_breakout');
  fixture.componentRef.setInput('canRedeploy', opts.canRedeploy);
  fixture.componentRef.setInput(
    'redeployQueryParams',
    opts.redeployQueryParams ?? {
      strategy_key: 'spy_15m_breakout',
      spec_path: '/specs/spy_15m_breakout.json',
    },
  );
  fixture.detectChanges();
  return {
    el: fixture.nativeElement as HTMLElement,
    component: fixture.componentInstance,
  };
}

afterEach(() => TestBed.resetTestingModule());

describe('StrategyRulesCardComponent', () => {
  it('renders strategy / order mode / daily cap / sizing in the primary block', () => {
    const { el } = render({
      startDefaults: makeDefaults(),
      provenance: makeProvenance(),
      sizing: makeSizing(),
      canRedeploy: true,
    });
    const text = el.textContent ?? '';

    expect(text).toContain('Strategy Rules');
    expect(text).toContain('spy_15m_breakout');
    expect(text).toContain('Live submission');
    expect(text).toContain('3 orders / day');
    expect(text).toContain('Safe Canary');
  });

  it('shows the read-only submission label when start_defaults.readonly is true', () => {
    const { el } = render({
      startDefaults: makeDefaults({ readonly: true }),
      provenance: makeProvenance(),
      sizing: makeSizing(),
      canRedeploy: true,
    });

    expect(el.textContent ?? '').toContain('Read-only (no order submission)');
  });

  it('renders the [Redeploy with new rules] link when canRedeploy is true', () => {
    const { el } = render({
      startDefaults: makeDefaults(),
      provenance: makeProvenance(),
      sizing: makeSizing(),
      canRedeploy: true,
    });

    const link = el.querySelector<HTMLAnchorElement>(
      '[data-testid="redeploy-with-new-rules"]',
    );
    expect(link).not.toBeNull();
    expect(link?.getAttribute('href')).toContain('/broker/deploy');
  });

  it('hides the redeploy link and shows an explanatory note when canRedeploy is false', () => {
    const { el } = render({
      startDefaults: makeDefaults(),
      provenance: makeProvenance(),
      sizing: makeSizing(),
      canRedeploy: false,
    });

    expect(el.querySelector('[data-testid="redeploy-with-new-rules"]')).toBeNull();
    expect(el.textContent ?? '').toMatch(/Redeploy is available after the bot stops/i);
  });

  it('emits redeployRequested when the link is clicked', () => {
    const { el, component } = render({
      startDefaults: makeDefaults(),
      provenance: makeProvenance(),
      sizing: makeSizing(),
      canRedeploy: true,
    });
    let fired = 0;
    component.redeployRequested.subscribe(() => (fired += 1));

    el.querySelector<HTMLAnchorElement>(
      '[data-testid="redeploy-with-new-rules"]',
    )?.click();

    expect(fired).toBe(1);
  });

  it('lists advanced fields (broker address, hydration, contract path, SHA) in the disclosure', () => {
    const { el } = render({
      startDefaults: makeDefaults(),
      provenance: makeProvenance(),
      sizing: makeSizing(),
      canRedeploy: true,
    });
    const text = el.textContent ?? '';

    expect(text).toContain('host.containers.internal:7497');
    expect(text).toContain('require');
    expect(text).toContain('/specs/spy_15m_breakout.json');
    expect(text).toContain('b'.repeat(12));
    expect(text).toContain('qc_12345');
  });

  it('degrades gracefully when start_defaults and provenance are null', () => {
    const { el } = render({
      startDefaults: null,
      provenance: null,
      sizing: null,
      canRedeploy: false,
    });
    const text = el.textContent ?? '';

    expect(text).toContain('(unknown)');
    expect(text).toContain('(not recorded)');
  });

  it('labels a pre-policy (legacy ledger) run honestly when sizing.preset is null', () => {
    const { el } = render({
      startDefaults: makeDefaults(),
      provenance: makeProvenance(),
      sizing: makeSizing({ preset: null }),
      canRedeploy: true,
    });

    expect(el.textContent ?? '').toContain('Pre-policy run (legacy ledger)');
  });
});
