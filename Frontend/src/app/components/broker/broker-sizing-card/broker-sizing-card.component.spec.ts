import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';
import type { InstanceSizing } from '../../../api/live-instances.types';
import { BrokerSizingCardComponent } from './broker-sizing-card.component';

function render(sizing: InstanceSizing): HTMLElement {
  const fixture = TestBed.createComponent(BrokerSizingCardComponent);
  fixture.componentRef.setInput('sizing', sizing);
  fixture.detectChanges();
  return fixture.nativeElement as HTMLElement;
}

afterEach(() => TestBed.resetTestingModule());

describe('BrokerSizingCardComponent', () => {
  it('renders Safe canary facts with the live-config and live-override pills', () => {
    const text =
      render({
        policy: { kind: 'FixedShares', value: 1 },
        preset: 'safe_canary',
        governed_by: 'live_config',
        sizing_provenance: 'live_override',
        per_trade_audit: [],
      }).textContent ?? '';

    expect(text).toContain('Position sizing');
    expect(text).toContain('Safe canary');
    expect(text).toContain('1 share per signal');
    expect(text).toContain('Deploy-form policy');
    expect(text).toContain('Live override');
  });

  it('renders the per-trade audit list when rows are present', () => {
    const text =
      render({
        policy: { kind: 'FixedShares', value: 1 },
        preset: 'safe_canary',
        governed_by: 'live_config',
        sizing_provenance: 'live_override',
        per_trade_audit: [
          {
            ts_ms: 1700000000000,
            symbol: 'SPY',
            policy_kind: 'FixedShares',
            policy_value: '1',
            intended_qty: 1,
            reference_price: '500.25',
            sized_via: 'policy_set_holdings',
          },
        ],
      }).textContent ?? '';

    expect(text).toContain('Per-trade audit');
    expect(text).toContain('SPY');
    expect(text).toContain('FixedShares(1)');
    expect(text).toContain('500.25');
  });

  it('renders the honest "Pre-policy run" badge when the policy is absent', () => {
    const html = render({
      policy: null,
      preset: null,
      governed_by: 'live_config',
      sizing_provenance: 'live_override',
      per_trade_audit: [],
    });

    expect(html.textContent).toContain('Pre-policy run');
    // Static-fact rows are suppressed for legacy runs.
    expect(html.querySelectorAll('.facts .fact').length).toBe(0);
  });

  it('labels a StrategyExplicit policy as self-sized', () => {
    const text =
      render({
        policy: { kind: 'StrategyExplicit' },
        preset: 'explicit',
        governed_by: 'strategy_explicit',
        sizing_provenance: 'live_override',
        per_trade_audit: [],
      }).textContent ?? '';

    expect(text).toContain('Self-sized (strategy explicit)');
    expect(text).toContain('Strategy supplies its own quantity');
    expect(text).toContain('Strategy code');
  });

  it('renders SetHoldings as a fraction-of-portfolio target', () => {
    const text =
      render({
        policy: { kind: 'SetHoldings', fraction: '1.0' },
        preset: 'reference_parity',
        governed_by: 'live_config',
        sizing_provenance: 'reference_native',
        per_trade_audit: [],
      }).textContent ?? '';

    expect(text).toContain('Reference parity');
    expect(text).toContain('Target 1.0 of portfolio value');
    expect(text).toContain('Matches QC audit copy');
  });
});
