import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import axe from 'axe-core';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { testLane } from '../fleet/fleet-directory-testing';
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

async function renderWith(lane: LaneDescriptor, state: LaneVerdictState) {
  const stateFor = (clerkId: string): LaneVerdictState =>
    clerkId === lane.clerk_id ? state : UNPOLLED_LANE_STATE;
  return render(AlpacaLiveBannerComponent, {
    inputs: { lane },
    providers: [{ provide: AlpacaLiveVerdictService, useValue: { stateFor } }],
  });
}

const PAPER_LANE = testLane({ clerk_id: 'clrk_paper', broker: 'alpaca', display_label: 'Paper' });
const LIVE_LANE = testLane({ clerk_id: 'clrk_live', broker: 'alpaca', display_label: 'Live' });

describe('AlpacaLiveBannerComponent', () => {
  it('renders nothing before the first read for this lane', async () => {
    await renderWith(PAPER_LANE, { verdict: null, lastError: null });
    expect(screen.queryByRole('status')).toBeNull();
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
  });

  it('keeps an explicit loud warning on screen when the last read failed, never a grey unknown', async () => {
    await renderWith(PAPER_LANE, { verdict: null, lastError: new Error('down') });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-undetermined');
    expect(status.className).not.toContain('is-unknown');
    expect(status.textContent).toContain('Mode unavailable');
    expect(status.textContent).toContain('assume real money');
  });

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
});
