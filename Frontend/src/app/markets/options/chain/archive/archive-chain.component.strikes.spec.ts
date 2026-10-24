import { fireEvent, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { SERVED_LIMITS } from '../../../../components/options-backtest/testing/fixtures';
import { reads } from '../../../../components/options-backtest/testing/page-actions';
import { PastChainInvalidError } from './past-chain.service';
import type { ExpiryQuery } from './past-chain.service';
import { chainExpiry } from './testing/fixtures';
import { chainTable, fakeChainService, openStandardChain, renderArchive, typeStrikes, typeStrikesAndStay } from './testing/render-archive';

const box = () => screen.getByLabelText('Strikes each side of the money');
const note = () => reads(document.getElementById('archive-chain-strikes-note'));
const lastQuery = (service: ReturnType<typeof fakeChainService>): ExpiryQuery => {
  const query = service.expiry.mock.lastCall?.[0];
  if (query === undefined) throw new Error('The chain was never read.');
  return query;
};
const askedWith = (service: ReturnType<typeof fakeChainService>) => service.expiry.mock.calls.map((call) => call[0].strikes);

async function open(limits = SERVED_LIMITS) {
  const service = fakeChainService(vi);
  await renderArchive(service, limits);
  await openStandardChain();
  return service;
}

describe('the strikes each side of the money', () => {
  it('start empty, which sends nothing and leaves the number to the server, and the box shows the number the server used', async () => {
    const service = await open();

    expect(lastQuery(service).strikes).toBeNull();
    expect(box().getAttribute('placeholder')).toBe('10');
    expect(note()).toBe("Empty means the server's own number. The most it takes: 14.");
  });

  it.each([
    ['3', '3'],
    ['14', '14'],
    ['03', '3'],
    ['0', '0'],
  ])('send what was typed, %j, as the digits %j', async (typed, sent) => {
    const service = await open();

    typeStrikes(typed);

    await waitFor(() => expect(lastQuery(service).strikes).toBe(sent));
  });

  it.each(['x', '3.5', '-1', '1e1', '3 4'])('refuse %j at the box without sending anything', async (typed) => {
    const service = await open();
    const before = service.expiry.mock.calls.length;

    typeStrikes(typed);

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Enter a whole number of strikes, or leave the box empty.');
    expect(service.expiry.mock.calls.length).toBe(before);
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
  });

  it('show the server words at the box for a number it refuses, take the table away meanwhile, and bring it back for a number it takes', async () => {
    const expiry = vi.fn(async (query: ExpiryQuery) => {
      if (query.strikes === '15') throw new PastChainInvalidError([{ loc: ['query', 'strikes'], msg: 'Input should be less than or equal to 14' }]);
      return chainExpiry();
    });
    await renderArchive(fakeChainService(vi, { expiry }), null);
    await openStandardChain();

    typeStrikes('15');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Input should be less than or equal to 14');
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    typeStrikes('14');
    expect(await screen.findByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    expect(screen.queryByText('Input should be less than or equal to 14')).toBeNull();
  });
});

describe('the strikes box and the most the server takes', () => {
  it('says the number the server sent, or nothing of a most while it has not sent one, and the server then judges alone', async () => {
    await open({ ...SERVED_LIMITS, max_strikes_each_side: 6 });
    expect(note()).toBe("Empty means the server's own number. The most it takes: 6.");
  });

  it('refuses a larger count at the box before asking, in words with the most in them, and takes the table away', async () => {
    const service = await open();
    const before = service.expiry.mock.calls.length;

    typeStrikes('15');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Strikes each side of the money: the most the server takes is 14. Enter 14 or fewer, or leave the box empty.');
    expect(box().getAttribute('aria-invalid')).toBe('true');
    expect(service.expiry.mock.calls.length).toBe(before);
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
  });

  it('asks for the most itself, and for a count below it, and the refusal goes when the count is one it takes', async () => {
    const service = await open();
    typeStrikes('15');
    await screen.findAllByRole('alert');

    typeStrikes('14');
    await waitFor(() => expect(lastQuery(service).strikes).toBe('14'));
    typeStrikes('9');
    await waitFor(() => expect(lastQuery(service).strikes).toBe('9'));

    expect(await screen.findByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    expect(screen.queryByText(/the most the server takes is/)).toBeNull();
  });

  it('judges by the limit the server sent: 9 is more than 8, and 8 is not', async () => {
    const service = await open({ ...SERVED_LIMITS, max_strikes_each_side: 8 });

    typeStrikes('9');
    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Strikes each side of the money: the most the server takes is 8. Enter 8 or fewer, or leave the box empty.');
    typeStrikes('8');

    await waitFor(() => expect(lastQuery(service).strikes).toBe('8'));
  });
});

describe('the strikes box being read', () => {
  it('asks once for what was typed, after a pause: 14 is read for 14 and never for 1', async () => {
    const service = await open();

    typeStrikesAndStay('1');
    await new Promise((done) => setTimeout(done, 100));
    typeStrikesAndStay('14');

    await waitFor(() => expect(lastQuery(service).strikes).toBe('14'));
    expect(askedWith(service)).toEqual([null, '14']);
  });

  it('asks at once when the box is left', async () => {
    const service = await open();

    typeStrikesAndStay('7');
    expect(askedWith(service)).toEqual([null]);
    fireEvent.blur(box());

    await waitFor(() => expect(lastQuery(service).strikes).toBe('7'), { timeout: 200 });
  });

  it('asks at once on Enter', async () => {
    const service = await open();

    typeStrikesAndStay('9');
    fireEvent.keyDown(box(), { key: 'Enter' });

    await waitFor(() => expect(lastQuery(service).strikes).toBe('9'), { timeout: 200 });
  });

  it('does not refuse half of what is being typed: the refusal is of what was committed', async () => {
    await open();

    typeStrikesAndStay('x');

    expect(screen.queryByText('Enter a whole number of strikes, or leave the box empty.')).toBeNull();
    expect(await screen.findByText('Enter a whole number of strikes, or leave the box empty.')).toBeTruthy();
    expect(chainTable).toBeDefined();
  });
});
