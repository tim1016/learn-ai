import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import { RuntimeBannerComponent } from './runtime-banner.component';
import type { OperatorSurfaceRuntimeFreshness } from '../../../../api/live-instances.types';

function withHeadline(): OperatorSurfaceRuntimeFreshness {
  const notice = {
    code: 'runtime.market_data_stale' as const,
    tier: 'warning' as const,
    title: 'Market data is stale',
    message:
      'The most recent bar is older than the freshness window. New trading decisions are held until fresh data arrives.',
    source_codes: ['BAR_LOOP_LATEST_BAR_STALE'],
    forensic_facts: { bar_loop_age_ms: 99_000 },
    action: { kind: 'wait' as const, label: null, target: null },
    runbook_slug: 'runtime-freshness',
    occurred_at_ms: null,
  };
  const fresh = { state: 'FRESH' as const, age_ms: 0, stale_reason_codes: [] };
  const stale = { state: 'STALE' as const, age_ms: 99_000, stale_reason_codes: ['BAR_LOOP_LATEST_BAR_STALE'] };
  return {
    posture_demoted: true,
    stale_reason_codes: ['BAR_LOOP_LATEST_BAR_STALE'],
    command_loop: fresh,
    broker: fresh,
    bar_loop: stale,
    control_plane: fresh,
    headline: notice,
    // Backend pre-filters: the headline is excluded from additional_reasons.
    additional_reasons: [],
  };
}

function freshFreshness(): OperatorSurfaceRuntimeFreshness {
  const fresh = { state: 'FRESH' as const, age_ms: 0, stale_reason_codes: [] };
  return {
    posture_demoted: false,
    stale_reason_codes: [],
    command_loop: fresh,
    broker: fresh,
    bar_loop: fresh,
    control_plane: fresh,
    headline: null,
    additional_reasons: [],
  };
}

describe('RuntimeBannerComponent', () => {
  it('renders nothing when there is no headline and no stale reasons', async () => {
    const { container } = await render(RuntimeBannerComponent, {
      inputs: { freshness: freshFreshness() },
    });
    expect(container.querySelector('[data-testid="runtime-banner"]')).toBeNull();
  });

  it('renders the headline as the primary OperatorNotice', async () => {
    await render(RuntimeBannerComponent, {
      inputs: { freshness: withHeadline() },
    });
    expect(screen.getByText('Market data is stale')).toBeTruthy();
    expect(
      screen.getByText(/most recent bar is older than the freshness window/i),
    ).toBeTruthy();
  });

  it('never renders raw stale_reason_codes as visible copy', async () => {
    const { container } = await render(RuntimeBannerComponent, {
      inputs: { freshness: withHeadline() },
    });
    expect(container.textContent).not.toContain('BAR_LOOP_LATEST_BAR_STALE');
  });

  it('renders nothing when only additional_reasons are present (no headline, e.g. session closed)', async () => {
    const f = freshFreshness();
    f.additional_reasons = [
      {
        code: 'runtime.market_closed',
        tier: 'info',
        title: 'Market closed',
        message: 'The bot is idle until the regular trading session opens.',
        source_codes: ['BAR_LOOP_SESSION_CLOSED'],
        forensic_facts: {},
        action: { kind: 'none', label: null, target: null },
        runbook_slug: null,
        occurred_at_ms: null,
      },
    ];
    await render(RuntimeBannerComponent, { inputs: { freshness: f } });
    // The banner is hidden because there is no headline (session-closed
    // is suppressed). Reasons surface elsewhere in the cockpit.
    expect(screen.queryByText('Market closed')).toBeNull();
  });
});
