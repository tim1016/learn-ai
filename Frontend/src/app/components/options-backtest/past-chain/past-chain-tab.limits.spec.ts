import { fireEvent, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { SetupLimits } from '../options-backtest.types';
import { reads } from '../testing/page-actions';
import { SERVED_LIMITS } from '../testing/fixtures';
import type { ExpiryQuery } from './past-chain.service';
import { chainExpiry } from './testing/fixtures';
import { fakeChainService, openStandardChain, renderChainTab, typeStrikes } from './testing/render-chain';

const box = () => screen.getByLabelText('Strikes each side of the money');
const note = () => reads(document.getElementById('ob-chain-strikes-note'));
const lastQuery = (service: ReturnType<typeof fakeChainService>): ExpiryQuery | undefined => service.expiry.mock.lastCall?.[0];

async function openWith(limits: SetupLimits | null) {
  const service = fakeChainService(vi);
  await renderChainTab(service, 'SPY', limits);
  await openStandardChain();
  return service;
}

describe('the strikes box and the most the server takes', () => {
  it.each([
    ['says the number the server sent, whatever it is', { ...SERVED_LIMITS, max_strikes_each_side: 6 }, "Empty means the server's own number. The most it takes: 6."],
    ['says nothing of a most while the page has no limits, and the server judges alone', null, "Empty means the server's own number."],
  ])('%s', async (_name, limits, words) => {
    await openWith(limits);

    expect(note()).toBe(words);
  });

  it('refuses a larger count at the box before asking, in words with the most in them, and takes the table away', async () => {
    const service = await openWith(SERVED_LIMITS);
    const before = service.expiry.mock.calls.length;

    typeStrikes('15');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Strikes each side of the money: the most the server takes is 14. Enter 14 or fewer, or leave the box empty.');
    expect(box().getAttribute('aria-invalid')).toBe('true');
    expect(service.expiry.mock.calls.length).toBe(before);
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
  });

  it('asks for the most itself, and for a count below it, and the refusal goes when the count is one it takes', async () => {
    const service = await openWith(SERVED_LIMITS);
    typeStrikes('15');
    await screen.findAllByRole('alert');

    typeStrikes('14');
    await waitFor(() => expect(lastQuery(service)?.strikes).toBe('14'));
    typeStrikes('9');
    await waitFor(() => expect(lastQuery(service)?.strikes).toBe('9'));

    expect(await screen.findByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    expect(screen.queryByText(/the most the server takes is/)).toBeNull();
  });

  it('judges by the limit the server sent: 9 is more than 8, and 8 is not', async () => {
    const service = await openWith({ ...SERVED_LIMITS, max_strikes_each_side: 8 });

    typeStrikes('9');
    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Strikes each side of the money: the most the server takes is 8. Enter 8 or fewer, or leave the box empty.');
    typeStrikes('8');

    await waitFor(() => expect(lastQuery(service)?.strikes).toBe('8'));
  });

  it('sends a count the server may still refuse when the page has no limits, and shows the server’s refusal', async () => {
    const service = await openWith(null);

    typeStrikes('15');

    await waitFor(() => expect(lastQuery(service)?.strikes).toBe('15'));
    expect(screen.queryByText(/the most the server takes is/)).toBeNull();
  });

  it('reads the limits when they arrive after the tab was made: a count that was sent is refused once the limit is known', async () => {
    const service = fakeChainService(vi, { expiry: vi.fn(async () => chainExpiry()) });
    const view = await renderChainTab(service, 'SPY', null);
    await openStandardChain();
    typeStrikes('15');
    await waitFor(() => expect(lastQuery(service)?.strikes).toBe('15'));

    await view.rerender({ inputs: { startingUnderlying: 'SPY', limits: SERVED_LIMITS }, partialUpdate: true });
    fireEvent.blur(box());

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Strikes each side of the money: the most the server takes is 14. Enter 14 or fewer, or leave the box empty.');
  });
});
