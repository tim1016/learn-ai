import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { GoldenOptionSetupsService } from '../golden-option-setups.service';
import { exceptionGoldenSetup, goldenSetup } from '../testing/golden-fixture';
import { GoldenSetupApprovalComponent } from './golden-setup-approval.component';

describe('Golden setup approval', () => {
  it('retains the owner exception, failed rule and exposure label on a ready approved version', async () => {
    const version = exceptionGoldenSetup();
    await render(GoldenSetupApprovalComponent, { inputs: { runId: version.source_run_id }, providers: [{ provide: GoldenOptionSetupsService, useValue: { list: async () => [version] } }] });
    expect(await screen.findByText('Owner exception', { exact: true })).toBeTruthy();
    expect(screen.getByText(/The final Sharpe hurdle was not met/)).toBeTruthy();
    expect(screen.getByText(/The final interval was used in recorded research/)).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Review source Options study' }).getAttribute('href')).toBe('/options-golden-search/' + version.evidence.options_search_study_id);
  });

  it('approves the chosen completed run and retries a lost response under the same key', async () => {
    const approve = vi.fn().mockRejectedValueOnce(new Error('response lost')).mockResolvedValue(goldenSetup());
    await render(GoldenSetupApprovalComponent, {
      inputs: { runId: 7 },
      providers: [{ provide: GoldenOptionSetupsService, useValue: { list: async () => [], approve } }],
    });
    const note = await screen.findByLabelText('Approval note');
    fireEvent.input(note, { target: { value: 'Reviewed the exact run.' } });
    fireEvent.click(screen.getByRole('button', { name: 'Approve golden setup' }));
    await screen.findByText(/could not be recorded/i);
    fireEvent.click(screen.getByRole('button', { name: 'Approve golden setup' }));
    await screen.findByText(/Choose this version in an account/);

    expect(approve).toHaveBeenCalledTimes(2);
    expect(approve.mock.calls[0]).toEqual(approve.mock.calls[1]);
    expect(approve.mock.calls[0][0]).toBe(7);
    expect(approve.mock.calls[0][1]).toBe('Reviewed the exact run.');
    expect(screen.getByText('Trade bars do not validate quote spreads or actual live fills.')).toBeTruthy();
  });

  it('re-proves a stale version and then records an explicit revocation', async () => {
    const list = vi.fn().mockImplementation(async (runId?: number) => runId === 7 ? [goldenSetup({ status: 'stale' })] : []);
    const reprove = vi.fn().mockResolvedValue(goldenSetup());
    const revoke = vi.fn().mockResolvedValue(goldenSetup({ status: 'revoked', status_explanation: 'New entries are blocked.' }));
    await render(GoldenSetupApprovalComponent, {
      inputs: { runId: 7 }, providers: [{ provide: GoldenOptionSetupsService, useValue: {
        list, reprove, revoke,
      } }],
    });
    fireEvent.click(await screen.findByRole('button', { name: 'Re-prove from retained evidence' }));
    await screen.findByText(/Choose this version in an account/);
    fireEvent.input(screen.getByLabelText('Reason to revoke this version'), { target: { value: 'Stop using this evidence' } });
    fireEvent.click(screen.getByRole('button', { name: 'Revoke golden setup' }));
    await waitFor(() => expect(revoke).toHaveBeenCalledWith(goldenSetup().id, 'Stop using this evidence', expect.any(String)));
    await screen.findByText(/New entries are blocked/);
    expect(screen.queryByRole('button', { name: 'Re-prove from retained evidence' })).toBeNull();
  });
});
