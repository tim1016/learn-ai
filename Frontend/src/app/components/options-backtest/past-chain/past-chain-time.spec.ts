import { describe, expect, it } from 'vitest';

import { chosenMinute, tradingDateMsOf } from './past-chain-time';
import { HALF_DAY_CLOSE_MS, HALF_DAY_MS, HALF_DAY_OPEN_MS, MINUTE_1545_MS, SESSION_CLOSE_MS, SESSION_OPEN_MS, TRADING_DATE_MS } from './testing/fixtures';

const FULL_DAY = { openMs: SESSION_OPEN_MS, closeMs: SESSION_CLOSE_MS };
const HALF = { openMs: HALF_DAY_OPEN_MS, closeMs: HALF_DAY_CLOSE_MS };

describe('the trading date the routes are asked about', () => {
  it('is New York midnight of the picked date, follows the clock change, and is nothing while the field holds no usable date', () => {
    // 2026-10-09 is in daylight time, UTC-4: midnight is 04:00 UTC.
    expect.soft(tradingDateMsOf('2026-10-09'), 'is New York midnight of the picked date (1 of 2)').toBe(1_791_518_400_000);
    expect.soft(tradingDateMsOf('2026-10-09'), 'is New York midnight of the picked date (2 of 2)').toBe(TRADING_DATE_MS);

    // Friday 2026-03-06 is UTC-5 (05:00 UTC); Monday 2026-03-09 is UTC-4 (04:00 UTC) after the Sunday change.
    expect.soft(tradingDateMsOf('2026-03-06'), 'follows the clock change: the Friday before it is UTC-5 and the Monday after it is UTC-4 (1 of 2)').toBe(1_772_773_200_000);
    expect.soft(tradingDateMsOf('2026-03-09'), 'follows the clock change: the Friday before it is UTC-5 and the Monday after it is UTC-4 (2 of 2)').toBe(1_773_028_800_000);

    expect.soft(tradingDateMsOf(''), 'is nothing while the field holds no usable date').toBeNull();
  });
});

describe('the minute the owner types', () => {
  it('is read against the session of its day: the close by default, the minute as an instant, the open and the close themselves, a refusal outside the session, the early close of a half day', () => {
    expect.soft(chosenMinute('', TRADING_DATE_MS, FULL_DAY), 'is the session close, the server own default, while nothing is typed (1 of 2)').toEqual({ kind: 'close' });
    expect.soft(chosenMinute('   ', TRADING_DATE_MS, FULL_DAY), 'is the session close, the server own default, while nothing is typed (2 of 2)').toEqual({ kind: 'close' });

    // 15:45 is 945 minutes past midnight: 04:00 UTC + 15 h 45 min = 19:45 UTC.
    expect.soft(chosenMinute('15:45', TRADING_DATE_MS, FULL_DAY), 'is that minute of that New York day, as an instant on a whole minute (1 of 2)').toEqual({ kind: 'at', atMs: 1_791_575_100_000 });
    expect.soft(chosenMinute('15:45', TRADING_DATE_MS, FULL_DAY), 'is that minute of that New York day, as an instant on a whole minute (2 of 2)').toEqual({ kind: 'at', atMs: MINUTE_1545_MS });

    expect.soft(chosenMinute('09:30', TRADING_DATE_MS, FULL_DAY), 'may be the open itself and the close itself (1 of 2)').toEqual({ kind: 'at', atMs: 1_791_552_600_000 });
    expect.soft(chosenMinute('16:00', TRADING_DATE_MS, FULL_DAY), 'may be the open itself and the close itself (2 of 2)').toEqual({ kind: 'at', atMs: 1_791_576_000_000 });

    const chosen = chosenMinute('09:29', TRADING_DATE_MS, FULL_DAY);

    expect.soft(chosen, 'is refused a minute before the open, with the session of that day in words').toEqual({ kind: 'refused', problem: 'That is before the open. Enter a time from 09:30 ET to 16:00 ET.' });

    expect.soft(chosenMinute('16:01', TRADING_DATE_MS, FULL_DAY), 'is refused a minute after the close').toEqual({ kind: 'refused', problem: 'That is after the close. Enter a time from 09:30 ET to 16:00 ET.' });

    // The day after Thanksgiving closes at 13:00: midnight 05:00 UTC, so 13:00 is 18:00 UTC.
    expect.soft(chosenMinute('13:00', HALF_DAY_MS, HALF), 'is held to the early close of a half day, which the session of that day states and no constant does (1 of 3)').toEqual({ kind: 'at', atMs: 1_795_802_400_000 });
    expect.soft(chosenMinute('13:01', HALF_DAY_MS, HALF), 'is held to the early close of a half day, which the session of that day states and no constant does (2 of 3)').toEqual({ kind: 'refused', problem: 'That is after the close. Enter a time from 09:30 ET to 13:00 ET.' });
    expect.soft(chosenMinute('15:00', HALF_DAY_MS, HALF), 'is held to the early close of a half day, which the session of that day states and no constant does (3 of 3)').toMatchObject({ kind: 'refused' });
  });

  it('is read on the New York calendar while no session is known: across a clock change, waiting for a date, and sent unchecked', () => {
    // Monday 2026-03-09 is UTC-4: 09:30 is 13:30 UTC, so midnight 1_773_028_800_000 plus 34 200 000.
    expect.soft(chosenMinute('09:30', 1_773_028_800_000, null), 'keeps a minute in the New York day when the clocks have just changed (1 of 2)').toEqual({ kind: 'at', atMs: 1_773_063_000_000 });
    // Friday 2026-03-06 is UTC-5: 16:00 is 21:00 UTC, so midnight 1_772_773_200_000 plus 57 600 000.
    expect.soft(chosenMinute('16:00', 1_772_773_200_000, null), 'keeps a minute in the New York day when the clocks have just changed (2 of 2)').toEqual({ kind: 'at', atMs: 1_772_830_800_000 });

    expect.soft(chosenMinute('10:00', null, null), 'waits for a date when a minute is typed and there is none to put it on').toEqual({ kind: 'waiting' });

    expect.soft(chosenMinute('03:00', TRADING_DATE_MS, null), 'is sent unchecked while the session of the day is not known, and the server judges it').toEqual({ kind: 'at', atMs: 1_791_529_200_000 });
  });

  it.each(['9:30', 'abc', '24:00', '12:60', '12:30:15', '1230'])('is refused as not hours and minutes when it reads %j', (typed) => {
    expect(chosenMinute(typed, TRADING_DATE_MS, FULL_DAY)).toEqual({ kind: 'refused', problem: 'Enter a time as hours and minutes.' });
  });
});
