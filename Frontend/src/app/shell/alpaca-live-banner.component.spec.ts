import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import axe from 'axe-core';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { provideFleetDirectory, testLane } from '../fleet/fleet-directory-testing';
import type { LaneDescriptor } from '../fleet/fleet-directory.types';
import {
  AlpacaLiveVerdictService,
  UNPOLLED_LANE_STATE,
  type LaneVerdictState,
} from '../services/alpaca-live-verdict.service';
import { AlpacaLiveBannerComponent } from './alpaca-live-banner.component';

function verdict(overrides: Partial<AlpacaLiveVerdict>): AlpacaLiveVerdict {
  return {
    configured_mode: 'paper',
    observed_account_id: 'PA9',
    mode_agreement: 'agreed',
    clerk_authority: 'sqlite',
    clerk_refusal_reason_code: null,
    armed_instance_count: 0,
    envelope_state: 'not_applicable',
    // A paper verdict carries no envelope and no hold, and the server says so
    // with 'not_applicable' on both. The live cases below opt in explicitly.
    envelope_agreement: 'not_applicable',
    shadow_state: 'not_applicable',
    loss_hold: 'not_applicable',
    final_verdict: 'paper',
    headline: 'Paper account PA9 — no real money at risk',
    detail: 'ALPACA_MODE=paper.',
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

async function renderWith(
  lane: LaneDescriptor | null,
  state: LaneVerdictState,
  siblingLanes: readonly LaneDescriptor[] = [],
) {
  const stateFor = (clerkId: string): LaneVerdictState =>
    clerkId === lane?.clerk_id ? state : UNPOLLED_LANE_STATE;
  return render(AlpacaLiveBannerComponent, {
    inputs: { lane },
    providers: [
      { provide: AlpacaLiveVerdictService, useValue: { stateFor } },
      // Siblings for disambiguation now come from `FleetDirectoryService`
      // (`lanesOf`), not a prop — an explicit, empty-by-default directory so
      // a test that doesn't care about collisions never accidentally gets
      // one from `provideFleetDirectory()`'s own default fixture lane.
      provideFleetDirectory({ observed_at_ms: 1, clerks: [...siblingLanes] }),
    ],
  });
}

const PAPER_LANE = testLane({ clerk_id: 'clrk_paper', broker: 'alpaca', display_label: 'Paper' });
const LIVE_LANE = testLane({ clerk_id: 'clrk_live', broker: 'alpaca', display_label: 'Live' });

describe('AlpacaLiveBannerComponent', () => {
  it('warns loudly before the first read for this lane, never renders nothing', async () => {
    await renderWith(PAPER_LANE, { verdict: null, lastError: null });

    const status = screen.getByRole('status');
    expect(status.className).toContain('is-undetermined');
    expect(status.textContent).toContain('Paper');
    expect(status.textContent).toContain('assume real money');
  });

  it('warns loudly when there is no lane at all, because the roster itself is unknown', async () => {
    await renderWith(null, UNPOLLED_LANE_STATE);

    const status = screen.getByRole('status');
    expect(status.className).toContain('is-undetermined');
    expect(status.textContent).toContain('Alpaca lanes unknown');
    expect(status.textContent).toContain('assume real money');
  });

  it('renders paper mode as a compact Paper money chip labeled with its lane', async () => {
    await renderWith(PAPER_LANE, { verdict: verdict({}), lastError: null });
    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Paper');
    expect(status.textContent).toContain('Paper money');
    expect(status.getAttribute('title')).toBe('ALPACA_MODE=paper.');
    expect(status.className).toContain('is-paper');

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it('renders a live-unarmed account loudly with the lane label, account id, and armed count', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        envelope_state: 'configured_unsealed',
        envelope_agreement: 'unsealed',
        shadow_state: 'none',
        final_verdict: 'live-unarmed',
        headline: 'LIVE account 9LIVE0001 — real money, no instance armed',
        detail: 'Every order path refuses.',
      }),
      lastError: null,
    });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-live-unarmed');
    expect(status.textContent).toContain('Live');
    expect(status.textContent).toContain('9LIVE0001');
    expect(status.textContent).toContain('0 armed');
  });

  it('shows the loss hold on a live account when held', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        envelope_state: 'configured_unsealed',
        envelope_agreement: 'unsealed',
        shadow_state: 'none',
        final_verdict: 'live-unarmed',
        headline: 'LIVE account 9LIVE0001 — real money, no instance armed',
        detail: 'Every order path refuses.',
        loss_hold: 'held',
      }),
      lastError: null,
    });
    expect(screen.getByRole('status').textContent).toContain('loss hold');
  });

  it('shows nothing extra on a live account when the loss hold is clear', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        envelope_state: 'configured_unsealed',
        envelope_agreement: 'unsealed',
        shadow_state: 'none',
        final_verdict: 'live-unarmed',
        headline: 'LIVE account 9LIVE0001 — real money, no instance armed',
        detail: 'Every order path refuses.',
        loss_hold: 'clear',
      }),
      lastError: null,
    });
    expect(screen.getByRole('status').textContent).not.toContain('loss hold');
  });

  it('renders a server-reported unknown verdict as a loud warning that says assume real money', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: null,
        mode_agreement: 'disagreed',
        clerk_refusal_reason_code: 'LIVE_MODE_DISAGREEMENT',
        final_verdict: 'unknown',
        headline: 'Live mode configured — account state unknown',
        detail: 'the configured mode and the observed account disagree',
      }),
      lastError: null,
    });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-undetermined');
    expect(status.className).not.toContain('is-unknown');
    expect(status.textContent).toContain('assume real money');
    expect(status.textContent).toContain('Live Mode Disagreement');

    // The amber treatment's own markup, never axe-run before this round.
    // `color-contrast` stays off because jsdom computes no layout or cascade,
    // so axe cannot evaluate it here — asserting it would be a check that
    // cannot fail. The remaining rules do apply to this path.
    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it('keeps an explicit loud warning on screen when the last read failed, never a grey unknown', async () => {
    await renderWith(PAPER_LANE, { verdict: null, lastError: new Error('down') });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-undetermined');
    expect(status.className).not.toContain('is-unknown');
    expect(status.textContent).toContain('Mode unavailable');
    expect(status.textContent).toContain('assume real money');
  });

  it.each([
    [
      'the last read failed',
      PAPER_LANE,
      { verdict: null, lastError: new Error('down') } satisfies LaneVerdictState,
    ],
    [
      'no read has completed yet',
      PAPER_LANE,
      UNPOLLED_LANE_STATE,
    ],
    [
      'the server itself reports unknown',
      LIVE_LANE,
      {
        verdict: verdict({
          configured_mode: 'live',
          observed_account_id: null,
          final_verdict: 'unknown',
          headline: 'Live mode configured — account state unknown',
        }),
        lastError: null,
      } satisfies LaneVerdictState,
    ],
    ['there is no lane at all', null, UNPOLLED_LANE_STATE],
  ])(
    'carries the real-money assumption in the ACCESSIBLE NAME when %s (WCAG 1.4.1)',
    async (_cause, lane, state) => {
      await renderWith(lane, state);

      // `aria-label` wins the accessible-name computation, so a screen reader
      // announcing this live region by name must still hear the assumption —
      // it cannot survive in the visible text and the amber alone.
      const name = screen.getByRole('status').getAttribute('aria-label');
      expect(name).toContain('Assume real money');
      if (lane) expect(name).toContain(lane.display_label);
    },
  );

  it('renders a live-armed account in the loudest treatment with the armed count', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        armed_instance_count: 1,
        envelope_state: 'sealed',
        envelope_agreement: 'agreed',
        shadow_state: 'complete',
        final_verdict: 'live-armed',
        headline: 'LIVE account 9LIVE0001 — 1 instance armed, nothing submitted yet',
        detail: 'No path submits a real-money order in this slice.',
      }),
      lastError: null,
    });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-live-armed');
    expect(status.textContent).toContain('9LIVE0001');
    expect(status.textContent).toContain('1 armed');
  });

  it("shows the lane's account nickname instead of its raw label when one is set", async () => {
    const named = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    await renderWith(named, { verdict: verdict({}), lastError: null });

    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Strategy lab');
    expect(status.querySelector('.alpaca-banner__lane')?.textContent).toBe('Strategy lab');
  });

  it("shows this lane's own label beside its name when another lane shares it (ADR 0064 Decision 5)", async () => {
    const paper = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    const live = testLane({
      clerk_id: 'clrk_live',
      display_label: 'Live',
      provider_summary: { account_nickname: '  strategy lab  ' },
    });
    await renderWith(paper, { verdict: verdict({}), lastError: null }, [paper, live]);

    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Strategy lab');
    expect(status.textContent).toContain('(Paper)');
  });

  // Two lanes sharing a display name are a deliberately supported state
  // (ADR 0064 Decision 5), not an edge case: without carrying the
  // disambiguator into `aria-label` too, both badges would announce
  // identically to a screen reader (WCAG 4.1.2 / axe landmark-unique
  // territory), even though their visible pills already read differently.
  it("carries the disambiguator into the paper lane's accessible name, not only its visible pill", async () => {
    const paper = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    const live = testLane({
      clerk_id: 'clrk_live',
      display_label: 'Live',
      provider_summary: { account_nickname: '  strategy lab  ' },
    });
    await renderWith(paper, { verdict: verdict({}), lastError: null }, [paper, live]);

    expect(screen.getByRole('status').getAttribute('aria-label')).toContain(
      'Strategy lab (Paper)',
    );
  });

  it("carries the disambiguator into the live lane's accessible name, not only its visible pill", async () => {
    const paper = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    const live = testLane({
      clerk_id: 'clrk_live',
      display_label: 'Live',
      provider_summary: { account_nickname: '  strategy lab  ' },
    });
    await renderWith(live, { verdict: verdict({ observed_account_id: 'PA9' }), lastError: null }, [
      paper,
      live,
    ]);

    expect(screen.getByRole('status').getAttribute('aria-label')).toContain(
      'strategy lab (Live)',
    );
  });

  it('carries the disambiguator into the accessible name on the undetermined path too (not just the verdict path)', async () => {
    const paper = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    const live = testLane({
      clerk_id: 'clrk_live',
      display_label: 'Live',
      provider_summary: { account_nickname: '  strategy lab  ' },
    });
    await renderWith(paper, { verdict: null, lastError: new Error('down') }, [paper, live]);

    const name = screen.getByRole('status').getAttribute('aria-label');
    expect(name).toContain('Strategy lab (Paper)');
    expect(name).toContain('Assume real money');
  });

  it('never refuses a duplicate name, it only disambiguates', async () => {
    const paper = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Solo' },
    });
    await renderWith(paper, { verdict: verdict({}), lastError: null }, [paper]);

    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Solo');
    expect(status.textContent).not.toContain('(Paper)');
  });
});
