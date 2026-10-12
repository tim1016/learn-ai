import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { pickSymbol } from '../../../shared/symbol-picker/testing/fake-picker-world';
import { paragraph, reads } from '../testing/page-actions';
import { PastChainInvalidError, PastChainRefusedError } from './past-chain.service';
import type { ExpiryQuery } from './past-chain.service';
import { chainDay, chainExpiry, HALF_DAY, HALF_DAY_CLOSE_MS, HALF_DAY_MS, HALF_DAY_OPEN_MS, MINUTE_1545_MS, TRADING_DATE, TRADING_DATE_MS } from './testing/fixtures';
import { chainTable, expiriesTable, fakeChainService, openStandardChain, pickDate, renderChainTab, typeStrikes, typeStrikesAndStay, typeTime } from './testing/render-chain';

const lastExpiryQuery = (service: ReturnType<typeof fakeChainService>): ExpiryQuery => {
  const query = service.expiry.mock.lastCall?.[0];
  if (query === undefined) throw new Error('The chain was never read.');
  return query;
};

describe('the time of day', () => {
  it('is the close of the session when nothing is typed, and the box says which time that is once the date is read', async () => {
    const service = fakeChainService(vi);
    await renderChainTab(service);
    await openStandardChain();

    expect(lastExpiryQuery(service).atMs).toBeNull();
    expect(reads(paragraph(/^Until you type a time/))).toBe('Until you type a time, the chain is read at the close, 16:00 ET');
  });

  it('is the minute typed, on that New York day, when it is inside the session', async () => {
    const service = fakeChainService(vi, { expiry: vi.fn(async (query: ExpiryQuery) => chainExpiry({ at_ms: query.atMs ?? 0 })) });
    await renderChainTab(service);
    await openStandardChain();

    typeTime('15:45');
    await waitFor(() => expect(lastExpiryQuery(service).atMs).toBe(MINUTE_1545_MS));

    // 15:45 on 2026-10-09 is 19:45 UTC: 04:00 UTC midnight plus 15 h 45 min.
    expect(lastExpiryQuery(service).atMs).toBe(1_791_575_100_000);
    await waitFor(() => expect(reads(paragraph(/^Priced as of/))).toBe('Priced as of 15:45 ET on 2026-10-09'));
  });

  const timeBox = () => screen.getByLabelText('Time of day (New York time)');
  /** What Chrome holds for `10:--`: no value, and a bad input. The page is told only that the box changed. */
  function typeHalfATime(): void {
    Object.defineProperty(timeBox(), 'validity', { value: { badInput: true }, configurable: true });
    fireEvent.input(timeBox(), { target: { value: '' } });
  }
  function finishTheTime(text: string): void {
    Object.defineProperty(timeBox(), 'validity', { value: { badInput: false }, configurable: true });
    typeTime(text);
  }
  const answeringAtTheTimeAsked = () => fakeChainService(vi, { expiry: vi.fn(async (query: ExpiryQuery) => chainExpiry({ at_ms: query.atMs ?? 0 })) });

  it('is not the close when the box is emptied after a time was typed: nothing is asked, the chain keeps the time it was read for, and the box says to enter one', async () => {
    const service = answeringAtTheTimeAsked();
    await renderChainTab(service);
    await openStandardChain();
    typeTime('15:45');
    await waitFor(() => expect(reads(paragraph(/^Priced as of/))).toBe('Priced as of 15:45 ET on 2026-10-09'));
    const asked = service.expiry.mock.calls.length;

    typeTime('');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Enter a complete time, hours and minutes. Until you do, the chain keeps the last time it was read for.');
    expect(timeBox().getAttribute('aria-invalid')).toBe('true');
    await new Promise((done) => setTimeout(done, 30));
    expect(service.expiry.mock.calls.length).toBe(asked);
    expect(chainTable()).toBeTruthy();
    expect(reads(paragraph(/^Priced as of/))).toBe('Priced as of 15:45 ET on 2026-10-09');
  });

  it('is read again from the time that is then typed, and the box stops saying so', async () => {
    const service = answeringAtTheTimeAsked();
    await renderChainTab(service);
    await openStandardChain();
    typeTime('15:45');
    await waitFor(() => expect(lastExpiryQuery(service).atMs).toBe(MINUTE_1545_MS));
    typeTime('');
    await screen.findAllByRole('alert');

    typeTime('15:46');

    await waitFor(() => expect(lastExpiryQuery(service).atMs).toBe(MINUTE_1545_MS + 60_000));
    expect(screen.queryByText(/Enter a complete time/)).toBeNull();
    expect(timeBox().getAttribute('aria-invalid')).toBe('false');
  });

  it('keeps the last time that could be read, and not one the session refused, when the box is then emptied', async () => {
    const service = answeringAtTheTimeAsked();
    await renderChainTab(service);
    await openStandardChain();
    typeTime('15:45');
    await waitFor(() => expect(lastExpiryQuery(service).atMs).toBe(MINUTE_1545_MS));
    typeTime('16:01');
    await screen.findByText(/The chain is not read while the time of day holds something it cannot be asked with/);

    typeTime('');

    await waitFor(() => expect(reads(paragraph(/^Priced as of/))).toBe('Priced as of 15:45 ET on 2026-10-09'));
    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Enter a complete time, hours and minutes. Until you do, the chain keeps the last time it was read for.');
  });

  it('is not the close when the browser holds half a time either: it keeps the time before it, and says to enter one', async () => {
    const service = answeringAtTheTimeAsked();
    await renderChainTab(service);
    await openStandardChain();
    typeTime('15:45');
    await waitFor(() => expect(reads(paragraph(/^Priced as of/))).toBe('Priced as of 15:45 ET on 2026-10-09'));
    const asked = service.expiry.mock.calls.length;

    typeHalfATime();

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Enter a complete time, hours and minutes. Until you do, the chain keeps the last time it was read for.');
    await new Promise((done) => setTimeout(done, 30));
    expect(service.expiry.mock.calls.length).toBe(asked);
    expect(reads(paragraph(/^Priced as of/))).toBe('Priced as of 15:45 ET on 2026-10-09');
    finishTheTime('15:50');
    await waitFor(() => expect(lastExpiryQuery(service).atMs).toBe(MINUTE_1545_MS + 5 * 60_000));
  });

  it('is not the close when half a time is typed before any other: the chain stays as it was read, at the close', async () => {
    const service = answeringAtTheTimeAsked();
    await renderChainTab(service);
    await openStandardChain();
    const asked = service.expiry.mock.calls.length;

    typeHalfATime();

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Enter a complete time, hours and minutes. Until you do, the chain keeps the last time it was read for.');
    await new Promise((done) => setTimeout(done, 30));
    expect(service.expiry.mock.calls.length).toBe(asked);
    expect(chainTable()).toBeTruthy();
  });

  it.each([
    ['09:29', 'That is before the open. Enter a time from 09:30 ET to 16:00 ET.'],
    ['16:01', 'That is after the close. Enter a time from 09:30 ET to 16:00 ET.'],
  ])('is refused at the box, with the session of that day in words, when it is %s, and the chain is not read for it', async (typed, problem) => {
    const service = fakeChainService(vi);
    await renderChainTab(service);
    await openStandardChain();
    const before = service.expiry.mock.calls.length;

    typeTime(typed);

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain(problem);
    expect(screen.getByLabelText('Time of day (New York time)').getAttribute('aria-invalid')).toBe('true');
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    expect(screen.getByText(/The chain is not read while the time of day holds something it cannot be asked with/)).toBeTruthy();
    expect(service.expiry.mock.calls.length).toBe(before);
  });

  it('is held to the early close of a half day, which the day answer states', async () => {
    const day = vi.fn(async () => chainDay({ session_open_ms: HALF_DAY_OPEN_MS, session_close_ms: HALF_DAY_CLOSE_MS }));
    const service = fakeChainService(vi, { day });
    await renderChainTab(service);

    pickDate(HALF_DAY);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });

    expect(reads(paragraph(/^Until you type a time/))).toBe('Until you type a time, the chain is read at the close, 13:00 ET');
    typeTime('14:00');
    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('That is after the close. Enter a time from 09:30 ET to 13:00 ET.');
  });

  it('on a half day, 12:59 is a minute of the session and is sent as that instant', async () => {
    const day = vi.fn(async () => chainDay({ session_open_ms: HALF_DAY_OPEN_MS, session_close_ms: HALF_DAY_CLOSE_MS }));
    const service = fakeChainService(vi, { day });
    await renderChainTab(service);
    pickDate(HALF_DAY);
    await screen.findByRole('table', { name: /Every expiry the listing held/ });
    within(expiriesTable()).getAllByRole('button', { name: /Show chain/ })[0]?.click();
    await screen.findByRole('table', { name: /Calls on the left/ });

    typeTime('12:59');

    // The day after Thanksgiving: midnight is 05:00 UTC, the close 13:00 is 1_795_802_400_000, and 12:59 is a minute before.
    await waitFor(() => expect(lastExpiryQuery(service).atMs).toBe(1_795_802_340_000));
    expect(lastExpiryQuery(service).tradingDateMs).toBe(HALF_DAY_MS);
  });

  it('is shown at the box in the server words when the server refuses it although the page let it through', async () => {
    const expiry = vi.fn(async (query: ExpiryQuery) => {
      if (query.atMs !== null) throw new PastChainRefusedError('OPTIONS_BACKTEST_INSTANT_OUTSIDE_SESSION', 'the instant is outside the session of 2026-10-09', 400);
      return chainExpiry();
    });
    await renderChainTab(fakeChainService(vi, { expiry }));
    await openStandardChain();

    typeTime('10:00');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('the instant is outside the session of 2026-10-09');
    expect(screen.getByLabelText('Time of day (New York time)').getAttribute('aria-invalid')).toBe('true');
  });
});

describe('the strikes each side of the money', () => {
  it('start empty, which sends nothing and leaves the number to the server, and the box shows the number the server used', async () => {
    const service = fakeChainService(vi);
    await renderChainTab(service);
    await openStandardChain();

    expect(lastExpiryQuery(service).strikes).toBeNull();
    expect(screen.getByLabelText('Strikes each side of the money').getAttribute('placeholder')).toBe('10');
    expect(screen.getByText("Empty means the server's own number.")).toBeTruthy();
  });

  it.each([
    ['1', '1 strike each side of the money'],
    ['0', '0 strikes each side of the money'],
    ['2', '2 strikes each side of the money'],
  ])('are counted in the words under the table: %j is %j', async (typed, words) => {
    const expiry = vi.fn(async (query: ExpiryQuery) => chainExpiry({ strikes_each_side: query.strikes === null ? 10 : Number(query.strikes) }));
    await renderChainTab(fakeChainService(vi, { expiry }));
    await openStandardChain();

    typeStrikes(typed);

    await waitFor(() => expect(reads(paragraph(/strikes? each side of the money$/))).toBe(words));
  });

  it.each([
    ['3', '3'],
    ['14', '14'],
    ['03', '3'],
    ['0', '0'],
    ['000', '0'],
  ])('send what was typed, %j, as the digits %j', async (typed, sent) => {
    const service = fakeChainService(vi);
    await renderChainTab(service);
    await openStandardChain();

    typeStrikes(typed);

    await waitFor(() => expect(lastExpiryQuery(service).strikes).toBe(sent));
  });

  it.each(['x', '3.5', '-1', '1e1', '3 4'])('refuse %j at the box without sending anything', async (typed) => {
    const service = fakeChainService(vi);
    await renderChainTab(service);
    await openStandardChain();
    const before = service.expiry.mock.calls.length;

    typeStrikes(typed);

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Enter a whole number of strikes, or leave the box empty.');
    expect(service.expiry.mock.calls.length).toBe(before);
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
  });

  it('show the server words at the box for a number it refuses, take the table away meanwhile, and bring it back for a number it takes', async () => {
    const expiry = vi.fn(async (query: ExpiryQuery) => {
      if (query.strikes === '15') {
        throw new PastChainInvalidError([{ loc: ['query', 'strikes'], msg: 'Input should be less than or equal to 14' }]);
      }
      return chainExpiry();
    });
    await renderChainTab(fakeChainService(vi, { expiry }));
    await openStandardChain();

    typeStrikes('15');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Input should be less than or equal to 14');
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    typeStrikes('14');
    expect(await screen.findByRole('table', { name: /Calls on the left/ })).toBeTruthy();
    expect(screen.queryByText('Input should be less than or equal to 14')).toBeNull();
  });

  it('show the server words at the box for a window that is too wide', async () => {
    const expiry = vi.fn(async (query: ExpiryQuery) => {
      if (query.strikes === '14') {
        throw new PastChainRefusedError('OPTIONS_BACKTEST_WINDOW_TOO_WIDE', 'the window holds 61 contracts, calls and puts together, and a download takes at most 60: ask for fewer strikes each side', 400);
      }
      return chainExpiry();
    });
    await renderChainTab(fakeChainService(vi, { expiry }));
    await openStandardChain();

    typeStrikes('14');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('the window holds 61 contracts, calls and puts together, and a download takes at most 60: ask for fewer strikes each side');
    expect(screen.getByLabelText('Strikes each side of the money').getAttribute('aria-invalid')).toBe('true');
  });
});

describe('the strikes box being read', () => {
  const askedWith = (service: ReturnType<typeof fakeChainService>) => service.expiry.mock.calls.map((call) => call[0].strikes);

  it('asks once for what was typed, after a pause: 14 is read for 14 and never for 1', async () => {
    const service = fakeChainService(vi);
    await renderChainTab(service);
    await openStandardChain();

    typeStrikesAndStay('1');
    await new Promise((done) => setTimeout(done, 100));
    typeStrikesAndStay('14');

    await waitFor(() => expect(lastExpiryQuery(service).strikes).toBe('14'));
    expect(askedWith(service)).toEqual([null, '14']);
  });

  it('asks at once when the box is left', async () => {
    const service = fakeChainService(vi);
    await renderChainTab(service);
    await openStandardChain();

    typeStrikesAndStay('7');
    expect(askedWith(service)).toEqual([null]);
    fireEvent.blur(screen.getByLabelText('Strikes each side of the money'));

    await waitFor(() => expect(lastExpiryQuery(service).strikes).toBe('7'), { timeout: 200 });
  });

  it('asks at once on Enter', async () => {
    const service = fakeChainService(vi);
    await renderChainTab(service);
    await openStandardChain();

    typeStrikesAndStay('9');
    fireEvent.keyDown(screen.getByLabelText('Strikes each side of the money'), { key: 'Enter' });

    await waitFor(() => expect(lastExpiryQuery(service).strikes).toBe('9'), { timeout: 200 });
  });

  it('does not refuse half of what is being typed: the refusal is of what was committed', async () => {
    await renderChainTab(fakeChainService(vi));
    await openStandardChain();

    typeStrikesAndStay('x');

    expect(screen.queryByText('Enter a whole number of strikes, or leave the box empty.')).toBeNull();
    expect(await screen.findByText('Enter a whole number of strikes, or leave the box empty.')).toBeTruthy();
  });
});

describe('the underlying', () => {
  it('is read again for the listing when it is changed, and the expiry that was open is put away, with the date and the time left as they were', async () => {
    const service = fakeChainService(vi);
    const view = await renderChainTab(service);
    await openStandardChain();
    typeTime('15:45');
    await waitFor(() => expect(lastExpiryQuery(service).atMs).toBe(MINUTE_1545_MS));
    const chains = service.expiry.mock.calls.length;

    pickSymbol(view.fixture, 'AAPL');

    await waitFor(() => expect(service.day).toHaveBeenCalledTimes(2));
    expect(service.day.mock.lastCall?.[0]).toMatchObject({ underlying: 'AAPL', tradingDateMs: TRADING_DATE_MS });
    expect(await screen.findByText('No expiry is open. Open one above to read its chain.')).toBeTruthy();
    expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull();
    expect(within(expiriesTable()).getAllByRole('button', { name: /Show chain/ }).every((button) => button.getAttribute('aria-pressed') === 'false')).toBe(true);
    expect((screen.getByLabelText('Trading date') as HTMLInputElement).value).toBe(TRADING_DATE);
    expect((screen.getByLabelText('Time of day (New York time)') as HTMLInputElement).value).toBe('15:45');
    await new Promise((done) => setTimeout(done, 30));
    expect(service.expiry.mock.calls.length).toBe(chains);
  });

  it('does not bring the old expiry back when the first underlying is picked again', async () => {
    const service = fakeChainService(vi);
    const view = await renderChainTab(service);
    await openStandardChain();
    pickSymbol(view.fixture, 'AAPL');
    await screen.findByText('No expiry is open. Open one above to read its chain.');

    pickSymbol(view.fixture, 'SPY');

    await waitFor(() => expect(service.day).toHaveBeenCalledTimes(3));
    expect(await screen.findByText('No expiry is open. Open one above to read its chain.')).toBeTruthy();
    expect(service.expiry).toHaveBeenCalledTimes(1);
  });

  it('says what picking a symbol the lake holds no bars for will do, since the chain is fetched by the download and not by the pick', async () => {
    await renderChainTab(fakeChainService(vi));

    expect(screen.getByText(/Picking a symbol the lake holds no bars for starts a backfill of its trade bars first/)).toBeTruthy();
    expect(screen.getByText(/The chain's own data is fetched only by Download missing data\./)).toBeTruthy();
  });

  it('is asked for a listing only once there is a date', async () => {
    const service = fakeChainService(vi);
    const view = await renderChainTab(service);

    pickSymbol(view.fixture, 'AAPL');
    await view.fixture.whenStable();

    expect(service.day).not.toHaveBeenCalled();
  });

  it('puts a refusal of it at the picker, in the server words', async () => {
    const day = vi.fn(async () => {
      throw new PastChainInvalidError([{ loc: ['query', 'underlying'], msg: "String should match pattern '^[A-Z]{1,5}$'" }]);
    });
    await renderChainTab(fakeChainService(vi, { day }));

    pickDate(TRADING_DATE);

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain("String should match pattern '^[A-Z]{1,5}$'");
    expect(screen.getByText('The server did not take the underlying; its reason is at that control.')).toBeTruthy();
  });
});

describe('the trading date', () => {
  it('puts a refusal of it at the date field, in the server words', async () => {
    const day = vi.fn(async () => {
      throw new PastChainInvalidError([{ loc: ['query', 'trading_date_ms'], msg: 'Input should be greater than or equal to 0' }]);
    });
    await renderChainTab(fakeChainService(vi, { day }));

    pickDate('1969-12-31');

    expect((await screen.findAllByRole('alert')).map((alert) => reads(alert))).toContain('Input should be greater than or equal to 0');
    expect(screen.getByText('The server did not take the trading date; its reason is at that control.')).toBeTruthy();
  });

  it('is nothing to read when the field is emptied: the listing and the chain go, and nothing more is asked', async () => {
    const service = fakeChainService(vi);
    await renderChainTab(service);
    await openStandardChain();
    const days = service.day.mock.calls.length;
    const chains = service.expiry.mock.calls.length;

    pickDate('');

    await waitFor(() => expect(screen.queryByRole('table', { name: /Calls on the left/ })).toBeNull());
    expect(screen.queryByRole('table', { name: /Every expiry the listing held/ })).toBeNull();
    expect(screen.getByText(/Pick an underlying and a trading date/)).toBeTruthy();
    expect(service.day.mock.calls.length).toBe(days);
    expect(service.expiry.mock.calls.length).toBe(chains);
  });
});
