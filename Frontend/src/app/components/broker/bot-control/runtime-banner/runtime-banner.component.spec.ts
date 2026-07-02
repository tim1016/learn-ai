import { render, screen } from '@testing-library/angular';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { RuntimeBannerComponent, STALE_DEBOUNCE_MS } from './runtime-banner.component';
import type { OperatorNotice, OperatorSurfaceRuntimeFreshness } from '../../../../api/live-instances.types';

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

function incidentNotice(): OperatorNotice {
  return {
    code: 'watchdog.flatten_timed_out',
    tier: 'critical',
    title: 'Flatten timed out',
    message: 'The flatten-and-pause watchdog halt timed out.',
    source_codes: [],
    forensic_facts: {},
    action: { kind: 'open_runbook', label: 'How to recover', target: 'watchdog-halt' },
    runbook_slug: 'watchdog-halt',
    occurred_at_ms: null,
  };
}

function criticalFreshnessNotice(): OperatorNotice {
  return {
    code: 'runtime.command_loop_unresponsive',
    tier: 'critical',
    title: 'Command loop unresponsive',
    message: 'The command loop has not heartbeated.',
    source_codes: ['COMMAND_LOOP_UNRESPONSIVE'],
    forensic_facts: {},
    action: { kind: 'open_runbook', label: 'Runbook', target: 'command-loop' },
    runbook_slug: 'command-loop',
    occurred_at_ms: null,
  };
}

/** Advance the fake clock past the debounce window and flush pending async
 *  work so the banner's computed signals re-evaluate against the new clock. */
async function advancePastDebounce(): Promise<void> {
  await vi.advanceTimersByTimeAsync(STALE_DEBOUNCE_MS + 1_000);
}

describe('RuntimeBannerComponent', () => {
  beforeEach(() => {
    vi.useFakeTimers({ shouldAdvanceTime: false });
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it('renders nothing when there is no headline and no stale reasons', async () => {
    const { container } = await render(RuntimeBannerComponent, {
      inputs: { freshness: freshFreshness(), incidentHeadline: null },
    });
    expect(container.querySelector('[data-testid="runtime-banner"]')).toBeNull();
  });

  it('holds the freshness headline back until staleness persists past the debounce window', async () => {
    const { container } = await render(RuntimeBannerComponent, {
      inputs: { freshness: withHeadline() },
    });
    // Immediately after the first stale observation, the banner is suppressed.
    expect(container.querySelector('[data-testid="runtime-banner"]')).toBeNull();

    await advancePastDebounce();

    expect(screen.getByText('Market data is stale')).toBeTruthy();
    expect(
      screen.getByText(/most recent bar is older than the freshness window/i),
    ).toBeTruthy();
  });

  it('never renders raw stale_reason_codes as visible copy', async () => {
    const { container } = await render(RuntimeBannerComponent, {
      inputs: { freshness: withHeadline() },
    });
    await advancePastDebounce();
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
    await advancePastDebounce();
    // The banner is hidden because there is no headline (session-closed
    // is suppressed). Reasons surface elsewhere in Bot Control.
    expect(screen.queryByText('Market closed')).toBeNull();
  });

  // Debounce-specific cases.

  it('renders critical-tier freshness headlines immediately, bypassing the debounce', async () => {
    const f = freshFreshness();
    f.headline = criticalFreshnessNotice();
    f.posture_demoted = true;
    await render(RuntimeBannerComponent, { inputs: { freshness: f } });
    expect(screen.getByText('Command loop unresponsive')).toBeTruthy();
  });

  it('emits operator notice actions from the freshness headline', async () => {
    const f = freshFreshness();
    const action = {
      kind: 'renew_control_plane_lease' as const,
      label: 'Renew control-plane lease',
      target: 'daemon_lease',
    };
    f.headline = {
      ...criticalFreshnessNotice(),
      code: 'runtime.control_plane_lease_stale',
      title: 'Control-plane lease is stale',
      action,
    };
    f.posture_demoted = true;
    let captured: typeof action | null = null;

    await render(RuntimeBannerComponent, {
      inputs: { freshness: f },
      on: { actionClicked: (a) => { captured = a as typeof action; } },
    });
    await screen.getByRole('button', { name: /renew control-plane lease/i }).click();

    expect(captured).toEqual(action);
  });

  it('resets the debounce when freshness recovers, so a brief blip never shows the banner', async () => {
    const stale = withHeadline();
    const fresh = freshFreshness();

    const { rerender, container } = await render(RuntimeBannerComponent, {
      inputs: { freshness: stale },
    });
    // Spend less than the debounce window in the stale state, then recover.
    await vi.advanceTimersByTimeAsync(STALE_DEBOUNCE_MS / 2);
    await rerender({ inputs: { freshness: fresh } });
    // Even if we now wait, the banner should not render — the timer was reset.
    await advancePastDebounce();
    expect(container.querySelector('[data-testid="runtime-banner"]')).toBeNull();
  });

  // PR 5 — incident_headline wiring

  it('renders the incident headline above the freshness headline when both are set', async () => {
    await render(RuntimeBannerComponent, {
      inputs: {
        freshness: withHeadline(),
        incidentHeadline: incidentNotice(),
      },
    });
    await advancePastDebounce();
    const incidentEl = document.querySelector('[data-testid="runtime-banner-incident"]');
    expect(incidentEl).not.toBeNull();
    expect(incidentEl?.textContent ?? '').toContain('Flatten timed out');
    // Freshness headline also rendered
    expect(screen.getByText('Market data is stale')).toBeTruthy();
    // Incident block appears before the freshness headline in the DOM
    const banner = document.querySelector('[data-testid="runtime-banner"]');
    const incidentPos = banner?.innerHTML.indexOf('runtime-banner-incident') ?? -1;
    const freshPos = banner?.innerHTML.indexOf('Market data is stale') ?? -1;
    expect(incidentPos).toBeLessThan(freshPos);
  });

  it('renders the banner when incidentHeadline is set but freshness is null', async () => {
    const { container } = await render(RuntimeBannerComponent, {
      inputs: {
        freshness: null,
        incidentHeadline: incidentNotice(),
      },
    });
    expect(container.querySelector('[data-testid="runtime-banner"]')).not.toBeNull();
    expect(screen.getByText('Flatten timed out')).toBeTruthy();
  });

  it('shows the incident block immediately and the freshness headline after the debounce', async () => {
    const { container } = await render(RuntimeBannerComponent, {
      inputs: {
        freshness: withHeadline(),
        incidentHeadline: null,
      },
    });
    // Incident absent and freshness still debounced.
    expect(container.querySelector('[data-testid="runtime-banner-incident"]')).toBeNull();
    expect(container.querySelector('[data-testid="runtime-banner"]')).toBeNull();

    await advancePastDebounce();
    expect(container.querySelector('[data-testid="runtime-banner-incident"]')).toBeNull();
    expect(screen.getByText('Market data is stale')).toBeTruthy();
  });
});
