import { signal } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import axe from 'axe-core';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { AlpacaLiveVerdictService } from '../services/alpaca-live-verdict.service';
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

async function renderWith(v: AlpacaLiveVerdict | null) {
  return render(AlpacaLiveBannerComponent, {
    providers: [{ provide: AlpacaLiveVerdictService, useValue: { verdict: signal(v), lastError: signal(null) } }],
  });
}

describe('AlpacaLiveBannerComponent', () => {
  it('renders nothing before the first verdict', async () => {
    await renderWith(null);
    expect(screen.queryByRole('status')).toBeNull();
  });

  it('renders paper mode as a compact Paper money chip', async () => {
    await renderWith(verdict({}));
    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Paper money');
    expect(status.getAttribute('title')).toBe('ALPACA_MODE=paper.');
    expect(status.className).toContain('is-paper');

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it('renders a live-unarmed account loudly with the account id and armed count', async () => {
    await renderWith(
      verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        envelope_state: 'configured_unsealed',
        envelope_agreement: 'unsealed',
        shadow_state: 'none',
        final_verdict: 'live-unarmed',
        headline: 'LIVE account 9LIVE0001 — real money, no instance armed',
        detail: 'Every order path refuses.',
      }),
    );
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-live-unarmed');
    expect(status.textContent).toContain('9LIVE0001');
    expect(status.textContent).toContain('0 armed');
    expect(status.textContent).toContain('Live');
  });

  it('shows the loss hold on a live account when held', async () => {
    await renderWith(
      verdict({
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
    );
    expect(screen.getByRole('status').textContent).toContain('loss hold');
  });

  it('shows nothing extra on a live account when the loss hold is clear', async () => {
    await renderWith(
      verdict({
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
    );
    expect(screen.getByRole('status').textContent).not.toContain('loss hold');
  });

  it('renders unknown as a warning that names the disagreement code', async () => {
    await renderWith(
      verdict({
        configured_mode: 'live',
        observed_account_id: null,
        mode_agreement: 'disagreed',
        clerk_refusal_reason_code: 'LIVE_MODE_DISAGREEMENT',
        final_verdict: 'unknown',
        headline: 'Live mode configured — account state unknown',
        detail: 'the configured mode and the observed account disagree',
      }),
    );
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-unknown');
    expect(status.textContent).toContain('Live Mode Disagreement');
  });

  it('keeps an explicit unavailable state on screen when the last read failed', async () => {
    await render(AlpacaLiveBannerComponent, {
      providers: [
        {
          provide: AlpacaLiveVerdictService,
          useValue: { verdict: signal(null), lastError: signal(new Error('down')) },
        },
      ],
    });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-unknown');
    expect(status.textContent).toContain('Mode unavailable');
  });

  it('renders a live-armed account in the loudest treatment with the armed count', async () => {
    await renderWith(
      verdict({
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
    );
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-live-armed');
    expect(status.textContent).toContain('9LIVE0001');
    expect(status.textContent).toContain('1 armed');
  });
});
