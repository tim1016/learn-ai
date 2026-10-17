import { screen, waitFor } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import type { SetupRequest } from './options-backtest.types';
import { SERVED_LIMITS } from './testing/fixtures';
import { addLeg, flip, inLeg, lettingGo, press, typeInto } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';

function lastBody(service: ReturnType<typeof fakeService>): SetupRequest {
  const call = service.describe.mock.lastCall;
  if (call === undefined) throw new Error('describe was never called');
  return call[0] as SetupRequest;
}

describe('editing a contract on its leg card', () => {
  it('moves across ATM, chooses a distant strike, and resets without rebuilding the leg', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();

    press('Leg 1 strike offset -1');
    await waitFor(() => expect(lastBody(service).legs[0].strike).toEqual({ kind: 'atm_offset', steps: -1 }));
    press('Leg 1 higher strike');
    await waitFor(() => expect(lastBody(service).legs[0].strike).toEqual({ kind: 'atm' }));
    press('Leg 1 higher strike');
    await waitFor(() => expect(lastBody(service).legs[0].strike).toEqual({ kind: 'atm_offset', steps: 1 }));

    typeInto('Leg 1 strike offset', '12');
    await waitFor(() => expect(lastBody(service).legs[0].strike).toEqual({ kind: 'atm_offset', steps: 12 }));
    press('Reset Leg 1 to ATM');
    await waitFor(() => expect(lastBody(service).legs[0]).toMatchObject({ leg_id: 'call', strike: { kind: 'atm' } }));
  });

  it('flips a covered leg to Sell and back, keeping its id, strike and trading rules', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    addLeg();
    press('Leg 2 strike offset 2');
    flip('Leg 2 stop loss');
    typeInto('Leg 2 stop loss value', '25');
    await waitFor(() => expect(lastBody(service).legs[1].stop_loss).toEqual({ kind: 'percent', value: '25' }));
    const original = lastBody(service).legs[1];

    press('Flip Leg 2 to Sell');
    await waitFor(() => expect(lastBody(service).legs[1]).toEqual({ ...original, side: 'sell' }));
    press('Flip Leg 2 to Buy');
    await waitFor(() => expect(lastBody(service).legs[1]).toEqual(original));
  });

  it('edits type, quantity and expiry in place, and copies the edited contract under its own id', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();

    await userEvent.selectOptions(screen.getByLabelText('Leg 1 option type'), 'put');
    typeInto('Leg 1 contracts', '3');
    await userEvent.selectOptions(screen.getByLabelText('Leg 1 expiry'), 'min_days');
    typeInto('Leg 1 minimum expiry days', '14');
    press('Leg 1 strike offset -2');
    press('Copy Leg 1');

    const contract = { right: 'put', contracts: 3, expiry: { kind: 'min_days', days: 14 }, strike: { kind: 'atm_offset', steps: -2 } };
    await waitFor(() => expect(lastBody(service).legs).toEqual([
      { ...contract, leg_id: 'call' },
      { ...contract, leg_id: 'put' },
    ]));
  });

  it('keeps invalid edits out of the request and lets the owner recover with the strike stepper', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    await waitFor(() => expect(lastBody(service).legs).toHaveLength(1));
    const calls = service.describe.mock.calls.length;

    typeInto('Leg 1 strike offset', '');
    typeInto('Leg 1 contracts', '1.5');
    await lettingGo();

    expect(service.describe.mock.calls.length).toBe(calls);
    expect(inLeg(1).getByRole('alert').textContent).toMatch(/whole number/);
    typeInto('Leg 1 contracts', '2');
    press('Leg 1 lower strike');
    await waitFor(() => expect(lastBody(service).legs[0]).toMatchObject({ contracts: 2, strike: { kind: 'atm_offset', steps: -1 } }));
  });

  it('holds the strike stepper at the served limit and permits a step back inside it', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    typeInto('Leg 1 strike offset', String(SERVED_LIMITS.max_strike_steps));

    expect(screen.getByRole<HTMLButtonElement>('button', { name: 'Leg 1 higher strike' }).disabled).toBe(true);
    press('Leg 1 lower strike');
    await waitFor(() => expect(lastBody(service).legs[0].strike).toEqual({ kind: 'atm_offset', steps: SERVED_LIMITS.max_strike_steps - 1 }));
  });
});
