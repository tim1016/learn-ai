import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { LiveGraduationReviewComponent } from './live-graduation-review.component';
import type { LiveGraduationPlan } from './live-graduation.service';

const PLAN: LiveGraduationPlan = {
  plan_id: 'a'.repeat(64),
  confirmation_token: 'a'.repeat(64),
  account_id: '318420190',
  created_at_ms: Date.now(),
  expires_at_ms: Date.now() + 300_000,
  broker_observed_at_ms: Date.now(),
  position_count: 0,
  open_order_count: 0,
  stopped_bot_ids: ['sh-ema-spy-0910'],
  backup_reference: 'accounts/alpaca/live/verified-backups/backup-1',
  daily_loss_fraction: 0.1,
  daily_loss_usd: 200,
  arming_max_sessions: 1,
  extended_hours_entry_bps: 0,
  extended_hours_exit_bps: 0,
  consequence: 'Graduation changes custody but deploys and arms nothing.',
};

async function renderReview(overrides: Partial<{ expired: boolean; busy: boolean; applying: boolean; acknowledged: boolean }> = {}) {
  return render(LiveGraduationReviewComponent, {
    inputs: {
      plan: PLAN,
      expired: false,
      busy: false,
      applying: false,
      acknowledged: false,
      ...overrides,
    },
  });
}

describe('LiveGraduationReviewComponent', () => {
  it('renders the sealed evidence facts and requires acknowledgement before confirming', async () => {
    await renderReview();

    expect(screen.getByText('Evidence is ready')).toBeTruthy();
    expect(screen.getByText('0 · flat')).toBeTruthy();
    expect(screen.getByText('0 · clear')).toBeTruthy();
    const confirm = screen.getByRole('button', { name: 'Graduate to Live and restart' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
  });

  it('enables confirmation once acknowledged, and emits confirmRequested on click', async () => {
    const confirmed = { called: false };
    await render(LiveGraduationReviewComponent, {
      inputs: { plan: PLAN, expired: false, busy: false, applying: false, acknowledged: true },
      on: { confirmRequested: () => { confirmed.called = true; } },
    });

    const confirm = screen.getByRole('button', { name: 'Graduate to Live and restart' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    expect(confirmed.called).toBe(true);
  });

  it('disables confirmation and shows the expired badge once the review expires', async () => {
    await renderReview({ expired: true, acknowledged: true });

    expect(screen.getByText('Expired — prepare again')).toBeTruthy();
    const confirm = screen.getByRole('button', { name: 'Graduate to Live and restart' }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);
  });

  it('emits refreshRequested from the refresh-evidence button', async () => {
    const refreshed = { called: false };
    await render(LiveGraduationReviewComponent, {
      inputs: { plan: PLAN, expired: false, busy: false, applying: false, acknowledged: false },
      on: { refreshRequested: () => { refreshed.called = true; } },
    });

    fireEvent.click(screen.getByRole('button', { name: 'Refresh evidence' }));
    expect(refreshed.called).toBe(true);
  });
});
