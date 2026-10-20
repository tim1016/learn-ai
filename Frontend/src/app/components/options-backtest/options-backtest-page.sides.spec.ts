import { ruleSwitch } from './testing/page-actions';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { ServedLeg, ServedSetup, SetupRequest } from './options-backtest.types';
import { noReentryExampleBody, servedSaved } from './testing/fixtures';
import { addLeg, choose, flip, inLeg, legCard, press, typeInto } from './testing/page-actions';
import { fakeService, renderPage } from './testing/render-page';

/** What a leg's side does on the page: the Buy or Sell switch, the line under it, the leg's card, and the request. */

function lastBody(service: ReturnType<typeof fakeService>): SetupRequest {
  const call = service.describe.mock.lastCall;
  if (call === undefined) throw new Error('describe was never called');
  return call[0] as SetupRequest;
}

const lineOf = (position: number) => legCard(position).querySelector('.leg__line')?.textContent ?? 'no contract line';
const sideList = () => screen.getByRole('complementary', { name: 'Saved setups' });

describe('the Buy or Sell switch', () => {
  it('is on Buy for a new leg, beside the call or put choice, with no claim that only buying is offered, and is followed by one line that says a sold leg needs a bought leg of the same kind to cover it', async () => {
    await renderPage(fakeService(vi));

    const switchGroup = screen.getByRole('group', { name: 'Buy or sell' });
    const line = document.getElementById(switchGroup.getAttribute('aria-describedby') ?? 'missing');

    expect.soft(within(switchGroup).getByRole<HTMLInputElement>('radio', { name: 'Buy' }).checked, 'is on Buy for a new leg').toBe(true);
    expect.soft(within(switchGroup).getByRole<HTMLInputElement>('radio', { name: 'Sell' }).checked, 'is not on Sell for a new leg').toBe(false);
    expect.soft(screen.queryByText(/only buying is offered/i), 'no longer says that only buying is offered').toBeNull();
    expect.soft(line?.textContent, 'is followed by a line saying a sold leg needs a bought leg of the same kind to cover it').toMatch(/sold leg needs a bought leg of the same kind.*to cover it/);
  });

  it('adds a sold leg that reads Sell, and sends the side it shows', async () => {
    const service = fakeService(vi);
    await renderPage(service);

    addLeg('Call');
    choose('Sell');
    addLeg('Call');

    await waitFor(() => expect(lastBody(service).legs).toHaveLength(2));
    expect(lineOf(1)).toMatch(/^Buy · 1 contract · Call/);
    expect(lineOf(2)).toMatch(/^Sell · 1 contract · Call/);
    const [bought, sold] = lastBody(service).legs;
    expect(bought).not.toHaveProperty('side');
    expect(sold).toMatchObject({ leg_id: 'call_2', side: 'sell', right: 'call', contracts: 1 });
  });

  it('keeps the choice for the next leg, and switching back to Buy sends a leg with no side', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg('Call');
    choose('Sell');
    addLeg('Call');

    choose('Buy');
    addLeg('Put');

    await waitFor(() => expect(lastBody(service).legs).toHaveLength(3));
    expect(lastBody(service).legs.map((leg) => leg.side ?? 'buy')).toEqual(['buy', 'sell', 'buy']);
    expect(lineOf(3)).toMatch(/^Buy · 1 contract · Put/);
  });

  it('gives a copy of a sold leg its side, with its rules', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg('Call');
    choose('Sell');
    addLeg('Call');
    flip('Leg 2 stop loss');
    typeInto('Leg 2 stop loss value', '150');

    press('Copy Leg 2');

    await waitFor(() => expect(lastBody(service).legs).toHaveLength(3));
    expect(lineOf(3)).toMatch(/^Sell/);
    expect(lastBody(service).legs[2]).toMatchObject({ side: 'sell', stop_loss: { kind: 'percent', value: '150' } });
  });
});

describe('a stop loss on a sold leg', () => {
  it('is sent as typed, above a hundred percent, and the page does not judge it', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg('Call');
    choose('Sell');
    addLeg('Call');

    flip('Leg 2 stop loss');
    typeInto('Leg 2 stop loss value', '250');

    await waitFor(() => expect(lastBody(service).legs[1]).toMatchObject({ side: 'sell', stop_loss: { kind: 'percent', value: '250' } }));
    expect(inLeg(2).queryByRole('alert')).toBeNull();
  });
});

describe('what the server refuses is shown in the server’s words', () => {
  it('a sold leg with nothing bought to cover it is the server’s sentence, naming the kind, in the summary', async () => {
    const service = fakeService(vi);
    await renderPage(service);

    choose('Sell');
    addLeg('Call');

    const summary = await screen.findByRole('region', { name: 'In plain words' });
    await waitFor(() => expect(summary.textContent).toContain('the legs sell more call contracts than they buy (1 sold, 0 bought)'));
    expect(summary.textContent).toContain('a sold call needs a bought call to cover it');
  });

  it('a bought call beside it clears the refusal, and the setup is read back with the sold leg’s side', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    choose('Sell');
    addLeg('Call');
    const summary = await screen.findByRole('region', { name: 'In plain words' });
    await waitFor(() => expect(summary.textContent).toContain('sell more call contracts than they buy'));

    choose('Buy');
    addLeg('Call');

    await waitFor(() => expect(summary.textContent).not.toContain('sell more call contracts than they buy'));
    await waitFor(() => expect(service.describe).toHaveBeenLastCalledWith(expect.objectContaining({ legs: expect.arrayContaining([expect.objectContaining({ side: 'sell' })]) })));
  });

  it('two expiry rules on a setup that sells are the server’s sentence, naming the legs', async () => {
    const service = fakeService(vi);
    await renderPage(service);
    addLeg('Call');
    choose('Sell');
    choose('Same day');
    addLeg('Call');

    const summary = await screen.findByRole('region', { name: 'In plain words' });

    await waitFor(() => expect(summary.textContent).toContain("needs every leg on one expiry rule"));
    expect(summary.textContent).toContain("leg 'call_2' differs from leg 'call'");
  });
});

describe('opening a stored setup that sells', () => {
  const shortCall: ServedLeg = {
    leg_id: 'short_call',
    side: 'sell',
    right: 'call',
    contracts: 1,
    expiry: { kind: 'this_week' },
    strike: { kind: 'atm_offset', steps: 2 },
    stop_loss: { kind: 'percent', value: '150' },
  };
  const longCall: ServedLeg = { leg_id: 'long_call', right: 'call', contracts: 1, expiry: { kind: 'this_week' }, strike: { kind: 'atm' } };
  const spread = (): ServedSetup => ({ ...noReentryExampleBody(), legs: [longCall, shortCall] });

  it('reads the side back into the form, with the leg card saying Sell and what is sent saying sell, and keeps a sold leg’s stop at its stored value, above a hundred percent', async () => {
    const service = fakeService(vi, { list: vi.fn(async () => [servedSaved('Call spread', spread())]) });
    await renderPage(service);

    fireEvent.click(await within(sideList()).findByRole('button', { name: /^Call spread/ }));
    await screen.findByRole('article', { name: 'Leg 2' });

    expect.soft(lineOf(1), 'the first leg card says Buy').toMatch(/^Buy · 1 contract · Call/);
    expect.soft(lineOf(2), 'the second leg card says Sell').toMatch(/^Sell · 1 contract · Call · Weekly expiry · 2 strikes above the money/);
    expect.soft(screen.getByLabelText<HTMLInputElement>('Leg 2 stop loss value').value, 'the sold leg’s stop value is its stored value, above a hundred percent').toBe('150');
    expect.soft((ruleSwitch('Leg 2 stop loss') as HTMLInputElement).checked, 'the sold leg’s stop loss switch is on').toBe(true);
    await waitFor(() => expect(lastBody(service).legs).toHaveLength(2));
    expect.soft(lastBody(service).legs, 'what is sent says sell for the sold leg').toEqual([longCall, shortCall]);
  });
});

describe('a re-entry on a leg with no stop of its own', () => {
  it('on a sold leg is said to come back when it is closed with its protection, as the setup’s sentences say', async () => {
    await renderPage(fakeService(vi));
    addLeg('Call');
    choose('Sell');
    addLeg('Call');

    flip('Leg 2 re-entry on SL');

    // The server takes this setup when the bought call has a stop: that stop closes the sold call with it, and the sold
    // call earns its re-entry. The hint must not say that nothing re-enters until the sold leg's own stop is on.
    const hint = inLeg(2).getByRole('status').textContent ?? '';
    expect(hint).toMatch(/closed with its protection \(a stop on the bought call that covers it takes it along\)/);
    expect(hint).not.toMatch(/nothing re-enters/);
  });

  it('on a bought leg is said to come back with its own stop, or when a complete square off closes it', async () => {
    await renderPage(fakeService(vi));
    addLeg('Call');

    flip('Leg 1 re-entry on SL');

    const hint = inLeg(1).getByRole('status').textContent ?? '';
    expect(hint).toMatch(/nothing re-enters until Stop loss is on, unless a complete square off closes this leg/);
  });
});
