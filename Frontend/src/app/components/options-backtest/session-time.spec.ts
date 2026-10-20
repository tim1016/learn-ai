import { describe, expect, it } from 'vitest';

import { anchorFor, clockTextOf, distanceInWords, exitAfterEntry, formatClockText, NOON_MINUTE_ET, parseClockText, storedTime } from './session-time';
import { SESSION_CLOCK } from './testing/fixtures';

const MINUTE = 60_000;
const { open_minute_et: OPEN, close_minute_et: CLOSE } = SESSION_CLOCK;

describe('typed clock text', () => {
  it.each([
    ['09:35', 575],
    ['00:00', 0],
    ['23:59', 1439],
    ['12:00', 720],
  ])('reads %s as minute %i of the day', (text, minute) => {
    expect(parseClockText(text)).toBe(minute);
    expect(formatClockText(minute)).toBe(text);
  });

  it.each(['', '9:35', '24:00', '09:60', '09:35:30', 'ab:cd', ' 09:35'])('does not read "%s" as a minute', (text) => {
    expect(parseClockText(text)).toBeNull();
  });
});

describe('entry is stored from the open', () => {
  it.each([
    ['five minutes in is five minutes from the open', OPEN + 5, 5 * MINUTE],
    ['one minute in is the earliest entry the setup allows', OPEN + 1, MINUTE],
    ['at the close is the whole session from the open, which the server’s summary judges against the exit', CLOSE, (CLOSE - OPEN) * MINUTE],
  ])('%s', (_name, minute, offset) => {
    expect(storedTime('entry', formatClockText(minute), SESSION_CLOCK)).toEqual({ ok: true, time: { anchor: 'open', offset_ms: offset } });
  });

  it('refuses an entry at the open with the earliest time that is allowed, and an entry before the open or after the close, naming the hours the clock serves', () => {
    const atOpen = storedTime('entry', formatClockText(OPEN), SESSION_CLOCK);
    expect.soft(atOpen, 'at the open is refused, with the earliest time that is allowed').toEqual({ ok: false, problem: `The entry must be at least a minute after the open: ${formatClockText(OPEN + 1)} or later.` });

    const beforeOpen = storedTime('entry', formatClockText(OPEN - 1), SESSION_CLOCK);
    expect.soft(beforeOpen.ok, 'one minute before the open is a problem for the field').toBe(false);
    expect.soft(beforeOpen, 'one minute before the open is a problem for the field, and names the open').toMatchObject({ problem: expect.stringContaining('before the open') });

    const afterClose = storedTime('entry', formatClockText(CLOSE + 1), SESSION_CLOCK);
    expect.soft(afterClose, 'one minute after the close is a problem for the field').toMatchObject({ ok: false, problem: expect.stringContaining('after the close') });
    expect.soft(afterClose, 'one minute after the close names the hours the clock serves').toMatchObject({ problem: expect.stringContaining(`${formatClockText(OPEN)} to ${formatClockText(CLOSE)}`) });
  });

  it('counted from the close, which only a loaded setup does, needs two minutes before it', () => {
    expect(storedTime('entry', formatClockText(CLOSE - 2), SESSION_CLOCK, 'close')).toEqual({ ok: true, time: { anchor: 'close', offset_ms: 2 * MINUTE } });
    expect(storedTime('entry', formatClockText(CLOSE - 1), SESSION_CLOCK, 'close')).toEqual({
      ok: false,
      problem: `The entry must be at least 2 minutes before the close, to leave room for the exit: ${formatClockText(CLOSE - 2)} or earlier.`,
    });
  });
});

describe('exit is stored from the close', () => {
  it.each([
    ['46 minutes before the close is 46 minutes from the close', CLOSE - 46, 46 * MINUTE],
    ['one minute before the close is the latest exit the setup allows', CLOSE - 1, MINUTE],
    ['at the open is the whole session from the close, which the server’s summary judges against the entry', OPEN, (CLOSE - OPEN) * MINUTE],
  ])('%s', (_name, minute, offset) => {
    expect(storedTime('exit', formatClockText(minute), SESSION_CLOCK)).toEqual({ ok: true, time: { anchor: 'close', offset_ms: offset } });
  });

  it('refuses an exit at the close with the latest time that is allowed, and an exit outside the session on either side', () => {
    const result = storedTime('exit', formatClockText(CLOSE), SESSION_CLOCK);

    expect.soft(result, 'at the close is refused, with the latest time that is allowed').toEqual({ ok: false, problem: `The exit must be at least a minute before the close: ${formatClockText(CLOSE - 1)} or earlier.` });
    expect.soft(storedTime('exit', formatClockText(OPEN - 1), SESSION_CLOCK).ok, 'one minute before the open is a problem').toBe(false);
    expect.soft(storedTime('exit', formatClockText(CLOSE + 1), SESSION_CLOCK).ok, 'one minute after the close is a problem').toBe(false);
  });

  it('counted from the open, which only a loaded setup does, needs two minutes after it', () => {
    expect(storedTime('exit', formatClockText(OPEN + 2), SESSION_CLOCK, 'open')).toEqual({ ok: true, time: { anchor: 'open', offset_ms: 2 * MINUTE } });
    expect(storedTime('exit', formatClockText(OPEN + 1), SESSION_CLOCK, 'open')).toEqual({
      ok: false,
      problem: `The exit must be at least 2 minutes after the open, to come after an entry: ${formatClockText(OPEN + 2)} or later.`,
    });
  });
});

describe('the exit must be after the entry when both are counted from the same end', () => {
  const from = (anchor: 'open' | 'close', minutes: number) => ({ anchor, offset_ms: minutes * MINUTE });

  it('is refused when the exit is not later, and says where the entry is; is allowed when the exit is later; is left to the server’s summary for an entry from the open and an exit from the close', () => {
    expect.soft(exitAfterEntry(from('open', 30), from('open', 30), SESSION_CLOCK), 'is refused when the exit is at the entry, and says where the entry is, from the open').toBe(`The exit must be after the entry, which is at ${formatClockText(OPEN + 30)}.`);
    expect.soft(exitAfterEntry(from('open', 30), from('open', 10), SESSION_CLOCK), 'is refused when the exit is before the entry').toMatch(/^The exit must be after the entry/);
    expect.soft(exitAfterEntry(from('close', 20), from('close', 30), SESSION_CLOCK), 'is refused when the exit is not later, and says where the entry is, from the close').toBe(`The exit must be after the entry, which is at ${formatClockText(CLOSE - 20)}.`);
    expect.soft(exitAfterEntry(from('open', 30), from('open', 31), SESSION_CLOCK), 'is allowed when the exit is later, from the open').toBeNull();
    expect.soft(exitAfterEntry(from('close', 30), from('close', 29), SESSION_CLOCK), 'is allowed when the exit is later, from the close').toBeNull();
    expect.soft(exitAfterEntry(from('open', 380), from('close', 380), SESSION_CLOCK), 'is left to the server’s summary for an entry from the open and an exit from the close').toBeNull();
  });
});

describe('"No re-entry after" follows the noon rule', () => {
  it('stores noon and the minutes before it from the open, the minutes after it from the close, keeps the anchor a loaded setup came with, and takes the open and the close themselves as inside', () => {
    expect.soft(storedTime('noReentry', formatClockText(NOON_MINUTE_ET), SESSION_CLOCK), 'noon itself is stored from the open').toEqual({
      ok: true,
      time: { anchor: 'open', offset_ms: (NOON_MINUTE_ET - OPEN) * MINUTE },
    });
    expect.soft(storedTime('noReentry', formatClockText(NOON_MINUTE_ET + 1), SESSION_CLOCK), 'one minute after noon is stored from the close').toEqual({
      ok: true,
      time: { anchor: 'close', offset_ms: (CLOSE - NOON_MINUTE_ET - 1) * MINUTE },
    });
    expect.soft(anchorFor('noReentry', NOON_MINUTE_ET - 1), 'one minute before noon is stored from the open').toBe('open');
    const afterNoon = formatClockText(OPEN + 5 * 60);
    expect.soft(storedTime('noReentry', afterNoon, SESSION_CLOCK, 'open'), 'keeps the anchor a loaded setup came with').toEqual({ ok: true, time: { anchor: 'open', offset_ms: 5 * 60 * MINUTE } });
    expect.soft(storedTime('noReentry', formatClockText(OPEN), SESSION_CLOCK), 'the open itself is inside').toEqual({ ok: true, time: { anchor: 'open', offset_ms: 0 } });
    expect.soft(storedTime('noReentry', formatClockText(CLOSE), SESSION_CLOCK), 'the close itself is inside').toEqual({ ok: true, time: { anchor: 'close', offset_ms: 0 } });
  });
});

describe('text that is not a time', () => {
  it('is a problem for the field, not a guess', () => {
    expect(storedTime('entry', '', SESSION_CLOCK)).toMatchObject({ ok: false });
    expect(storedTime('entry', '25:00', SESSION_CLOCK)).toMatchObject({ ok: false });
  });
});

describe('a stored distance read back as a clock time', () => {
  it('is the open plus the distance or the close minus it, the open and the close at a distance of nothing, and names no time when the distance runs past midnight either way', () => {
    expect.soft(clockTextOf({ anchor: 'open', offset_ms: 5 * MINUTE }, SESSION_CLOCK), 'is the open plus the distance').toBe(formatClockText(OPEN + 5));
    expect.soft(clockTextOf({ anchor: 'close', offset_ms: 46 * MINUTE }, SESSION_CLOCK), 'is the close minus the distance').toBe(formatClockText(CLOSE - 46));
    expect.soft(clockTextOf({ anchor: 'open', offset_ms: 0 }, SESSION_CLOCK), 'is the open at a distance of nothing').toBe(formatClockText(OPEN));
    expect.soft(clockTextOf({ anchor: 'close', offset_ms: 0 }, SESSION_CLOCK), 'is the close at a distance of nothing').toBe(formatClockText(CLOSE));
    expect.soft(clockTextOf({ anchor: 'open', offset_ms: 1000 * MINUTE }, SESSION_CLOCK), 'names no time when the distance from the open runs past midnight').toBe('');
    expect.soft(clockTextOf({ anchor: 'close', offset_ms: 1000 * MINUTE }, SESSION_CLOCK), 'names no time when the distance from the close runs past midnight').toBe('');
  });

  it('returns to the same stored time for every boundary that is allowed', () => {
    const cases = [
      ['entry', OPEN + 1],
      ['entry', CLOSE],
      ['exit', OPEN],
      ['exit', CLOSE - 1],
      ['noReentry', OPEN],
      ['noReentry', CLOSE],
      ['noReentry', NOON_MINUTE_ET],
      ['noReentry', NOON_MINUTE_ET + 1],
    ] as const;
    for (const [role, minute] of cases) {
      const stored = storedTime(role, formatClockText(minute), SESSION_CLOCK);
      if (!stored.ok) throw new Error(`${role} ${minute} should be inside the session`);
      expect(clockTextOf(stored.time, SESSION_CLOCK)).toBe(formatClockText(minute));
    }
  });
});

describe('a clock other than the usual one', () => {
  const odd = { open_minute_et: 600, close_minute_et: 900 };

  it('is the one every conversion follows', () => {
    expect(storedTime('entry', '10:01', odd)).toEqual({ ok: true, time: { anchor: 'open', offset_ms: MINUTE } });
    expect(storedTime('entry', '10:00', odd)).toMatchObject({ ok: false, problem: expect.stringContaining('10:01 or later') });
    expect(storedTime('exit', '14:55', odd)).toEqual({ ok: true, time: { anchor: 'close', offset_ms: 5 * MINUTE } });
    expect(storedTime('exit', '15:00', odd)).toMatchObject({ ok: false, problem: expect.stringContaining('14:59 or earlier') });
    expect(storedTime('entry', '09:59', odd).ok).toBe(false);
    expect(clockTextOf({ anchor: 'open', offset_ms: 90 * MINUTE }, odd)).toBe('11:30');
  });
});

describe('the distance in words', () => {
  it.each([
    [{ anchor: 'open', offset_ms: 0 }, 'at the open'],
    [{ anchor: 'open', offset_ms: MINUTE }, '1 minute after the open'],
    [{ anchor: 'open', offset_ms: 5 * MINUTE }, '5 minutes after the open'],
    [{ anchor: 'close', offset_ms: 0 }, 'at the close'],
    [{ anchor: 'close', offset_ms: MINUTE }, '1 minute before the close'],
    [{ anchor: 'close', offset_ms: 46 * MINUTE }, '46 minutes before the close'],
  ] as const)('says %j as "%s"', (time, words) => {
    expect(distanceInWords(time)).toBe(words);
  });
});
