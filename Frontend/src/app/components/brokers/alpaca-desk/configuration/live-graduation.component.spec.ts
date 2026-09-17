import { fireEvent, render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { FleetDirectoryService } from '../../../../fleet/fleet-directory.service';
import { LiveGraduationComponent } from './live-graduation.component';
import {
  LiveGraduationService,
  type LiveGraduationPlan,
  type LiveGraduationStatus,
} from './live-graduation.service';

const ACCOUNT = '318420190';
const STATUS: LiveGraduationStatus = {
  account_id: ACCOUNT,
  configured_mode: 'live',
  authority: 'shadow',
  state: 'review_available',
  headline: 'Shadow authority is active',
  detail: 'Review current evidence before graduation.',
  next_action: 'Review changes nothing.',
  restart_managed: true,
};
const PLAN: LiveGraduationPlan = {
  plan_id: 'a'.repeat(64),
  confirmation_token: 'a'.repeat(64),
  account_id: ACCOUNT,
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

async function renderGraduation() {
  const service = {
    readStatus: vi.fn().mockResolvedValue(STATUS),
    prepare: vi.fn().mockResolvedValue(PLAN),
    apply: vi.fn().mockResolvedValue({ state: 'restart_scheduled' }),
  };
  const view = await render(LiveGraduationComponent, {
    inputs: { clerkId: 'clrk_live', accountId: ACCOUNT },
    providers: [
      { provide: LiveGraduationService, useValue: service },
      {
        provide: FleetDirectoryService,
        useValue: {
          lane: () => ({ effective_binding_generation: 4, routing_epoch: 7 }),
        },
      },
    ],
  });
  await view.fixture.whenStable();
  return { ...view, service };
}

describe('LiveGraduationComponent', () => {
  it('keeps graduation distinct from deploy and arming, then requires an explicit acknowledgement', async () => {
    const { fixture, service } = await renderGraduation();

    expect(screen.getByText(/does not deploy a strategy/)).toBeTruthy();
    fireEvent.click(await screen.findByRole('button', { name: 'Review Live graduation' }));
    await fixture.whenStable();

    expect(await screen.findByText('Evidence is ready')).toBeTruthy();
    expect(screen.getByText('0 · flat')).toBeTruthy();
    expect(screen.getByText('0 · clear')).toBeTruthy();
    const confirm = screen.getByRole('button', {
      name: 'Graduate to Live and restart',
    }) as HTMLButtonElement;
    expect(confirm.disabled).toBe(true);

    fireEvent.click(screen.getByRole('checkbox'));
    expect(confirm.disabled).toBe(false);
    fireEvent.click(confirm);
    await vi.waitFor(() => expect(service.apply).toHaveBeenCalledOnce());
    await fixture.whenStable();
    fixture.detectChanges();

    expect(await screen.findByText(/Restarting the clerk safely/)).toBeTruthy();
  });

  it('refuses loudly, not silently, when the review expired since the last render', async () => {
    // reviewExpired() reads Date.now() directly rather than a signal, so a
    // zoneless OnPush pass will not have re-rendered the disabled/expired
    // state purely because wall-clock time passed with no other
    // interaction. Only Date.now is mocked (not the whole clock), so the
    // DOM's [disabled] binding stays exactly as stale as production would
    // leave it, while confirm() must still refuse out loud on click.
    const dateSpy = vi.spyOn(Date, 'now');
    try {
      const { fixture, service } = await renderGraduation();
      fireEvent.click(await screen.findByRole('button', { name: 'Review Live graduation' }));
      await fixture.whenStable();
      expect(await screen.findByText('Evidence is ready')).toBeTruthy();
      fireEvent.click(screen.getByRole('checkbox'));

      dateSpy.mockReturnValue(PLAN.expires_at_ms + 1_000);

      const confirm = screen.getByRole('button', { name: 'Graduate to Live and restart' });
      expect((confirm as HTMLButtonElement).disabled).toBe(false);
      fireEvent.click(confirm);

      expect(service.apply).not.toHaveBeenCalled();
      expect(await screen.findByText(/review expired/i)).toBeTruthy();
    } finally {
      dateSpy.mockRestore();
    }
  });

  it('has no detectable accessibility violations in the review state', async () => {
    const { fixture } = await renderGraduation();
    fireEvent.click(await screen.findByRole('button', { name: 'Review Live graduation' }));
    await fixture.whenStable();

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });
    expect(results.violations).toEqual([]);
  });
});
