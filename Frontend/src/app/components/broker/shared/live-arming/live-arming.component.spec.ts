import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';
import { resourceTarget } from '../../../../fleet/resource-target';
import { AlpacaLiveVerdictService } from '../../../../services/alpaca-live-verdict.service';
import { LiveArmingComponent } from './live-arming.component';
import { LiveArmingService, type ArmingPlan, type ArmingStatus } from './live-arming.service';

const target = resourceTarget('alpaca', 'clrk_spec', { accountId: 'LIVE1', bindingGeneration: 1, routingEpoch: 2 });
const status: ArmingStatus = { account_id: 'LIVE1', strategy_instance_id: 'bot-a', state: 'unarmed',
  sessions_remaining: 0, armed_instance_count: 0, reason_code: null, observed_at_ms: 1_788_361_200_000 };
const plan: ArmingPlan = { account_id: 'LIVE1', strategy_instance_id: 'bot-a', plan_id: 'a'.repeat(64),
  confirmation_token: 'a'.repeat(64), created_at_ms: status.observed_at_ms, expires_at_ms: status.observed_at_ms + 120_000,
  envelope: { loss_fraction: .1, loss_usd: 100, shadow_sessions: 1, arming_max_sessions: 3, xh_entry_bps: 10, xh_exit_bps: 20 },
  exit_terms: { exit_allowance_bps: 20, band_multiple: 2, spread_cap_bps: 50, provenance: 'deployed' },
  changes: ['Exit allowance changed to 20 bps'], shadow_receipt_sha256: null };

async function setup() {
  const service = { status: vi.fn().mockResolvedValue(status), prepare: vi.fn().mockResolvedValue(plan),
    apply: vi.fn().mockResolvedValue({ ...status, state: 'armed', armed_instance_count: 1, sessions_remaining: 3 }),
    disarm: vi.fn().mockResolvedValue({ ...status, state: 'disarmed' }) };
  const refresh = vi.fn().mockResolvedValue(undefined);
  const view = await render(LiveArmingComponent, { inputs: { target, sid: 'bot-a' }, providers: [
    { provide: LiveArmingService, useValue: service }, { provide: AlpacaLiveVerdictService, useValue: { refresh } },
  ] });
  await view.fixture.whenStable();
  return { ...view, service, refresh };
}

describe('Live arming', () => {
  it('requires typed confirmation, updates the account count and can disarm', async () => {
    const { fixture, service, refresh } = await setup();
    fireEvent.click(screen.getByRole('button', { name: 'Review and arm' }));
    await screen.findByText('This bot’s exit allowance');
    expect((screen.getByRole('button', { name: 'Arm this live bot' }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.input(screen.getByLabelText('Confirmation token'), { target: { value: plan.confirmation_token } });
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Arm this live bot' }));
    await vi.waitFor(() => expect(service.apply).toHaveBeenCalledOnce());
    await screen.findByText(/1 armed on this account/);
    expect(refresh).toHaveBeenCalledOnce();
    fireEvent.click(screen.getByRole('button', { name: 'Disarm' }));
    await vi.waitFor(() => expect(service.disarm).toHaveBeenCalledOnce());
    await screen.findByText(/0 armed on this account/);
  });

  it('shows a small loss fraction without rounding it to zero', async () => {
    const { service } = await setup();
    service.prepare.mockResolvedValue({ ...plan, envelope: { ...plan.envelope, loss_fraction: 0.00025 } });
    fireEvent.click(screen.getByRole('button', { name: 'Review and arm' }));
    expect(await screen.findByText(/fraction 0\.00025/)).toBeTruthy();
  });

  it('reads fresh status when returning to a previously armed bot', async () => {
    const { fixture, service } = await setup();
    fireEvent.click(screen.getByRole('button', { name: 'Review and arm' }));
    await screen.findByText('This bot’s exit allowance');
    fireEvent.input(screen.getByLabelText('Confirmation token'), { target: { value: plan.confirmation_token } });
    fixture.detectChanges();
    fireEvent.click(screen.getByRole('button', { name: 'Arm this live bot' }));
    await screen.findByText(/1 armed on this account/);

    service.status.mockResolvedValue({ ...status, strategy_instance_id: 'bot-b' });
    fixture.componentRef.setInput('sid', 'bot-b');
    fixture.detectChanges();
    await fixture.whenStable();
    service.status.mockResolvedValue({ ...status, state: 'disarmed' });
    fixture.componentRef.setInput('sid', 'bot-a');
    fixture.detectChanges();
    await fixture.whenStable();
    await screen.findByText(/0 armed on this account/);
    expect(screen.queryByText(/1 armed on this account/)).toBeNull();
    expect(service.status).toHaveBeenLastCalledWith(target, 'bot-a');
  });

  it('still offers disarm when current configuration prevents reading status', async () => {
    const { fixture, service } = await setup();
    service.status.mockRejectedValue(new Error('Configuration unavailable'));
    service.disarm.mockResolvedValue({ ...status, strategy_instance_id: 'bot-b', state: 'disarmed', armed_instance_count: null });
    fixture.componentRef.setInput('sid', 'bot-b');
    fixture.detectChanges();
    await screen.findByText(/Arming status could not be read/);
    fireEvent.click(screen.getByRole('button', { name: 'Disarm' }));
    await vi.waitFor(() => expect(service.disarm).toHaveBeenCalledWith(target, 'bot-b'));
    await screen.findByText(/Account armed count unavailable/);
  });

  it('drops an old plan and its late response when the selected bot changes', async () => {
    const { fixture, service } = await setup();
    let answer!: (value: ArmingPlan) => void;
    service.prepare.mockImplementationOnce(() => new Promise(resolve => { answer = resolve; }));
    fireEvent.click(screen.getByRole('button', { name: 'Review and arm' }));
    fixture.componentRef.setInput('sid', 'bot-b');
    fixture.detectChanges();
    answer(plan);
    await fixture.whenStable();
    expect(screen.queryByLabelText('Confirmation token')).toBeNull();
    expect(service.apply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Review and arm' }));
    await vi.waitFor(() => expect(service.prepare).toHaveBeenLastCalledWith(target, 'bot-b'));
  });
});
