import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { MAX_LEGS } from './leg-draft';
import { formatClockText } from './session-time';
import { exampleBody, SESSION_CLOCK } from './testing/fixtures';
import { addLeg, choose, flip, inLeg, isDisabled, legCard, press, typeInto } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';
import type { LegRequest, SetupRequest } from './options-backtest.types';

const { open_minute_et: OPEN, close_minute_et: CLOSE } = SESSION_CLOCK;

function lastBody(service: ReturnType<typeof fakeService>): SetupRequest {
  const call = service.describe.mock.lastCall;
  if (call === undefined) throw new Error('describe was never called');
  return call[0] as SetupRequest;
}

function lastLeg(service: ReturnType<typeof fakeService>, position = 0): LegRequest {
  const leg = lastBody(service).legs[position];
  if (leg === undefined) throw new Error(`the body has no leg ${position + 1}`);
  return leg;
}

describe('building a setup', () => {
  it('shows the cards in the order the owner works down them', async () => {
    await renderPage(fakeService(vi));

    const headings = screen.getAllByRole('heading', { level: 2 }).map((heading) => heading.textContent);

    expect(headings).toEqual(['Instrument', 'Entry settings', 'Legwise settings', 'Leg builder', 'Legs', 'Costs', 'In plain words']);
  });

  it('sends exactly the shipped example when the owner builds it by hand', async () => {
    const service = fakeService(vi);
    await renderPage(service);

    typeInto('Exit time', formatClockText(CLOSE - 46));
    flip('No re-entry after');
    typeInto('No re-entry after time', formatClockText(CLOSE - 60));
    choose('Complete');
    addLeg('Call');
    addLeg('Put');
    for (const leg of [1, 2]) {
      flip(`Leg ${leg} stop loss`);
      typeInto(`Leg ${leg} stop loss value`, '25');
      flip(`Leg ${leg} simple momentum`);
      typeInto(`Leg ${leg} momentum value`, '25');
      flip(`Leg ${leg} re-entry on stop loss`);
    }

    await waitFor(() => expect(service.describe).toHaveBeenLastCalledWith(exampleBody()));
  });

  it('sends the other example when the owner shuts the re-entry door at the entry minute', async () => {
    const service = fakeService(vi);
    await renderPage(service);

    typeInto('Exit time', formatClockText(CLOSE - 46));
    flip('No re-entry after');
    typeInto('No re-entry after time', formatClockText(OPEN + 5));
    choose('Complete');
    addLeg('Call');
    addLeg('Put');
    for (const leg of [1, 2]) {
      flip(`Leg ${leg} stop loss`);
      typeInto(`Leg ${leg} stop loss value`, '25');
      flip(`Leg ${leg} simple momentum`);
      typeInto(`Leg ${leg} momentum value`, '25');
      flip(`Leg ${leg} re-entry on stop loss`);
    }

    await waitFor(() => expect(service.describe).toHaveBeenLastCalledWith(exampleBody({ anchor: 'open', offset_ms: 5 * 60_000 })));
  });
});

describe('a leg’s three rules', () => {
  it('a stop loss sends nothing until it is on and has a value, then sends what was chosen', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    await waitFor(() => expect(lastLeg(service)).not.toHaveProperty('stop_loss'));

    flip('Leg 1 stop loss');
    const callsBefore = service.describe.mock.calls.length;
    typeInto('Leg 1 stop loss value', '30');
    choose('Points');

    await waitFor(() => expect(lastLeg(service)).toMatchObject({ stop_loss: { kind: 'points', value: '30' } }));
    expect(service.describe.mock.calls.length).toBeGreaterThan(callsBefore);
  });

  it('a stop loss that is on without a value is a problem at its field, and nothing is sent for it', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    await waitFor(() => expect(service.describe).toHaveBeenCalled());
    const calls = service.describe.mock.calls.length;

    flip('Leg 1 stop loss');

    const field = screen.getByLabelText('Leg 1 stop loss value');
    expect(field.getAttribute('aria-invalid')).toBe('true');
    expect(inLeg(1).getByRole('alert').textContent).toMatch(/number/i);
    expect(service.describe.mock.calls.length).toBe(calls);
  });

  it('a momentum rule sends its kind and value, and nothing once it is switched off, with its fields greyed', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    flip('Leg 1 simple momentum');
    typeInto('Leg 1 momentum value', '4');
    choose('Points down');
    await waitFor(() => expect(lastLeg(service)).toMatchObject({ momentum: { kind: 'points_down', value: '4' } }));

    flip('Leg 1 simple momentum');

    await waitFor(() => expect(lastLeg(service)).not.toHaveProperty('momentum'));
    const value = screen.getByLabelText<HTMLInputElement>('Leg 1 momentum value');
    expect(value.disabled).toBe(true);
    expect(value.value).toBe('4');
    expect((screen.getByRole('group', { name: 'Leg 1 momentum direction' }) as HTMLFieldSetElement).disabled).toBe(true);
  });

  it('a re-entry sends its kind and its count as a number', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();

    flip('Leg 1 re-entry on stop loss');
    typeInto('Leg 1 re-entries', '3');

    await waitFor(() => expect(lastLeg(service)).toMatchObject({ reentry_on_stop: { kind: 'at_once', count: 3 } }));
  });

  it('shows the three rules switched off on a new leg, each with its fields greyed', async () => {
    await renderPage(fakeService(vi));
    addLeg();

    for (const field of ['Leg 1 stop loss value', 'Leg 1 momentum value', 'Leg 1 re-entries']) {
      expect(isDisabled(screen.getByLabelText(field))).toBe(true);
    }
    for (const rule of ['Leg 1 stop loss', 'Leg 1 simple momentum', 'Leg 1 re-entry on stop loss']) {
      expect((screen.getByRole('switch', { name: rule }) as HTMLInputElement).checked).toBe(false);
    }
  });
});

describe('re-entering on momentum', () => {
  const reentryKind = () => screen.getByRole('group', { name: 'Leg 1 re-entry kind' });

  it('cannot be chosen while the leg has no momentum rule, and the page says why', async () => {
    await renderPage(fakeService(vi));
    addLeg();
    flip('Leg 1 re-entry on stop loss');

    expect(within(reentryKind()).getByRole<HTMLInputElement>('radio', { name: 'On momentum' }).disabled).toBe(true);
    const reasonId = reentryKind().getAttribute('aria-describedby');
    expect(reasonId).not.toBeNull();
    expect(document.getElementById(reasonId ?? '')?.textContent?.trim()).not.toBe('');
  });

  it('can be chosen once momentum is on, and then needs no explanation', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    flip('Leg 1 stop loss');
    typeInto('Leg 1 stop loss value', '20');
    flip('Leg 1 simple momentum');
    typeInto('Leg 1 momentum value', '10');
    flip('Leg 1 re-entry on stop loss');

    const onMomentum = within(reentryKind()).getByRole<HTMLInputElement>('radio', { name: 'On momentum' });
    expect(onMomentum.disabled).toBe(false);
    expect(reentryKind().getAttribute('aria-describedby')).toBeNull();
    fireEvent.click(onMomentum);

    await waitFor(() => expect(lastLeg(service)).toMatchObject({ reentry_on_stop: { kind: 'on_momentum', count: 1 }, momentum: { kind: 'percent_up' } }));
  });

  it('switching momentum off from under it is a problem at the re-entry, not a silent change, and nothing is sent', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    flip('Leg 1 simple momentum');
    typeInto('Leg 1 momentum value', '10');
    flip('Leg 1 re-entry on stop loss');
    fireEvent.click(within(reentryKind()).getByRole('radio', { name: 'On momentum' }));
    await waitFor(() => expect(lastLeg(service)).toMatchObject({ reentry_on_stop: { kind: 'on_momentum' } }));
    const calls = service.describe.mock.calls.length;

    flip('Leg 1 simple momentum');

    expect(within(reentryKind()).getByRole<HTMLInputElement>('radio', { name: 'On momentum' }).checked).toBe(true);
    expect(inLeg(1).getAllByRole('alert').map((alert) => alert.textContent).join(' ')).toMatch(/momentum/i);
    expect(service.describe.mock.calls.length).toBe(calls);
  });
});

describe('copying and deleting legs', () => {
  it('a copy is a new leg with its own id, rules and all, beside the one it came from', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    flip('Leg 1 stop loss');
    typeInto('Leg 1 stop loss value', '25');

    press('Copy Leg 1');

    await waitFor(() => expect(lastBody(service).legs.map((leg) => leg.leg_id)).toEqual(['call', 'call_2']));
    expect(lastLeg(service, 1)).toMatchObject({ stop_loss: { kind: 'percent', value: '25' } });
    expect(legCard(2).textContent).toContain(legCard(1).querySelector('.leg__line')?.textContent ?? 'missing');
  });

  it('deleting a leg removes it from what is sent', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg('Call');
    addLeg('Put');
    await waitFor(() => expect(lastBody(service).legs).toHaveLength(2));

    press('Delete Leg 1');

    await waitFor(() => expect(lastBody(service).legs.map((leg) => leg.leg_id)).toEqual(['put']));
    expect(screen.queryByRole('article', { name: 'Leg 2' })).toBeNull();
  });

  it('holds at most eight legs: Add leg and Copy stop there, and a deletion frees them', async () => {
    const service = fakeService(vi);
    await renderPage(service);

    for (let count = 0; count < MAX_LEGS; count += 1) addLeg();

    await waitFor(() => expect(lastBody(service).legs).toHaveLength(MAX_LEGS));
    expect(isDisabled(screen.getByRole('button', { name: 'Add leg' }))).toBe(true);
    expect(isDisabled(screen.getByRole('button', { name: 'Copy Leg 1' }))).toBe(true);
    expect(screen.getByText(/at most 8 legs/i)).toBeTruthy();

    press('Delete Leg 8');

    expect(isDisabled(screen.getByRole('button', { name: 'Add leg' }))).toBe(false);
    expect(isDisabled(screen.getByRole('button', { name: 'Copy Leg 1' }))).toBe(false);
  });

  it('will not add a leg whose contract is out of range, and says so at the builder', async () => {
    const service = fakeService(vi);
    await renderPage(service);

    typeInto('Contracts', '5000');

    expect(isDisabled(screen.getByRole('button', { name: 'Add leg' }))).toBe(true);
    expect(screen.getByLabelText('Contracts').getAttribute('aria-invalid')).toBe('true');
  });

  it('adds a leg from steps and days the owner chose, and sends them', async () => {
    const service = fakeService(vi);
    await renderPage(service);

    choose('Put');
    choose('At least N days');
    typeInto('Days (N)', '3');
    choose('Steps from the money');
    typeInto('Steps (+ above, - below)', '-2');
    typeInto('Contracts', '4');
    press('Add leg');

    await waitFor(() =>
      expect(lastLeg(service)).toEqual({
        leg_id: 'put',
        right: 'put',
        contracts: 4,
        expiry: { kind: 'min_days', days: 3 },
        strike: { kind: 'atm_offset', steps: -2 },
      }),
    );
  });
});

describe('typing a value key by key', () => {
  it('keeps the same field under the cursor, and sends the whole value', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg();
    flip('Leg 1 stop loss');
    const field = screen.getByLabelText<HTMLInputElement>('Leg 1 stop loss value');

    await userEvent.type(field, '12.5');

    expect(screen.getByLabelText('Leg 1 stop loss value')).toBe(field);
    expect(document.activeElement).toBe(field);
    expect(field.value).toBe('12.5');
    await waitFor(() => expect(lastLeg(service)).toMatchObject({ stop_loss: { value: '12.5' } }));
  });

  it('does the same in a card that is not about a leg', async () => {
    await renderPage(fakeService(vi));
    const cash = screen.getByLabelText<HTMLInputElement>('Cash');

    await userEvent.clear(cash);
    await userEvent.type(cash, '2500.75');

    expect(screen.getByLabelText('Cash')).toBe(cash);
    expect(cash.value).toBe('2500.75');
  });
});
