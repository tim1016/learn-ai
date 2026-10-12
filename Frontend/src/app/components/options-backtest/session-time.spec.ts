import { describe, expect, it } from 'vitest';

import { anchorFor, clockTextOf, distanceInWords, formatClockText, NOON_MINUTE_ET, parseClockText, storedTime } from './session-time';
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
  it('at the open is a distance of nothing', () => {
    expect(storedTime('entry', formatClockText(OPEN), SESSION_CLOCK)).toEqual({ ok: true, time: { anchor: 'open', offset_ms: 0 } });
  });

  it('five minutes in is five minutes from the open', () => {
    expect(storedTime('entry', formatClockText(OPEN + 5), SESSION_CLOCK)).toEqual({ ok: true, time: { anchor: 'open', offset_ms: 5 * MINUTE } });
  });

  it('at the close is the whole session from the open', () => {
    expect(storedTime('entry', formatClockText(CLOSE), SESSION_CLOCK)).toEqual({ ok: true, time: { anchor: 'open', offset_ms: (CLOSE - OPEN) * MINUTE } });
  });

  it('one minute before the open is a problem for the field', () => {
    const result = storedTime('entry', formatClockText(OPEN - 1), SESSION_CLOCK);
    expect(result.ok).toBe(false);
    expect(result).toMatchObject({ problem: expect.stringContaining('before the open') });
  });

  it('one minute after the close is a problem for the field, and names the hours the clock serves', () => {
    const result = storedTime('entry', formatClockText(CLOSE + 1), SESSION_CLOCK);
    expect(result).toMatchObject({ ok: false, problem: expect.stringContaining('after the close') });
    expect(result).toMatchObject({ problem: expect.stringContaining(`${formatClockText(OPEN)} to ${formatClockText(CLOSE)}`) });
  });
});

describe('exit is stored from the close', () => {
  it('at the close is a distance of nothing', () => {
    expect(storedTime('exit', formatClockText(CLOSE), SESSION_CLOCK)).toEqual({ ok: true, time: { anchor: 'close', offset_ms: 0 } });
  });

  it('46 minutes before the close is 46 minutes from the close', () => {
    expect(storedTime('exit', formatClockText(CLOSE - 46), SESSION_CLOCK)).toEqual({ ok: true, time: { anchor: 'close', offset_ms: 46 * MINUTE } });
  });

  it('at the open is the whole session from the close', () => {
    expect(storedTime('exit', formatClockText(OPEN), SESSION_CLOCK)).toEqual({ ok: true, time: { anchor: 'close', offset_ms: (CLOSE - OPEN) * MINUTE } });
  });

  it('outside the session on either side is a problem', () => {
    expect(storedTime('exit', formatClockText(OPEN - 1), SESSION_CLOCK).ok).toBe(false);
    expect(storedTime('exit', formatClockText(CLOSE + 1), SESSION_CLOCK).ok).toBe(false);
  });
});

describe('"No re-entry after" follows the noon rule', () => {
  it('noon itself is stored from the open', () => {
    expect(storedTime('noReentry', formatClockText(NOON_MINUTE_ET), SESSION_CLOCK)).toEqual({
      ok: true,
      time: { anchor: 'open', offset_ms: (NOON_MINUTE_ET - OPEN) * MINUTE },
    });
  });

  it('one minute after noon is stored from the close', () => {
    expect(storedTime('noReentry', formatClockText(NOON_MINUTE_ET + 1), SESSION_CLOCK)).toEqual({
      ok: true,
      time: { anchor: 'close', offset_ms: (CLOSE - NOON_MINUTE_ET - 1) * MINUTE },
    });
  });

  it('one minute before noon is stored from the open', () => {
    expect(anchorFor('noReentry', NOON_MINUTE_ET - 1)).toBe('open');
  });

  it('keeps the anchor a loaded setup came with', () => {
    const afterNoon = formatClockText(OPEN + 5 * 60);
    expect(storedTime('noReentry', afterNoon, SESSION_CLOCK, 'open')).toEqual({ ok: true, time: { anchor: 'open', offset_ms: 5 * 60 * MINUTE } });
  });

  it('the open and the close themselves are inside', () => {
    expect(storedTime('noReentry', formatClockText(OPEN), SESSION_CLOCK)).toEqual({ ok: true, time: { anchor: 'open', offset_ms: 0 } });
    expect(storedTime('noReentry', formatClockText(CLOSE), SESSION_CLOCK)).toEqual({ ok: true, time: { anchor: 'close', offset_ms: 0 } });
  });
});

describe('text that is not a time', () => {
  it('is a problem for the field, not a guess', () => {
    expect(storedTime('entry', '', SESSION_CLOCK)).toMatchObject({ ok: false });
    expect(storedTime('entry', '25:00', SESSION_CLOCK)).toMatchObject({ ok: false });
  });
});

describe('a stored distance read back as a clock time', () => {
  it('is the open plus the distance, or the close minus it', () => {
    expect(clockTextOf({ anchor: 'open', offset_ms: 5 * MINUTE }, SESSION_CLOCK)).toBe(formatClockText(OPEN + 5));
    expect(clockTextOf({ anchor: 'close', offset_ms: 46 * MINUTE }, SESSION_CLOCK)).toBe(formatClockText(CLOSE - 46));
  });

  it('is the open and the close at a distance of nothing', () => {
    expect(clockTextOf({ anchor: 'open', offset_ms: 0 }, SESSION_CLOCK)).toBe(formatClockText(OPEN));
    expect(clockTextOf({ anchor: 'close', offset_ms: 0 }, SESSION_CLOCK)).toBe(formatClockText(CLOSE));
  });

  it('names no time when the distance runs past midnight either way', () => {
    expect(clockTextOf({ anchor: 'open', offset_ms: 1000 * MINUTE }, SESSION_CLOCK)).toBe('');
    expect(clockTextOf({ anchor: 'close', offset_ms: 1000 * MINUTE }, SESSION_CLOCK)).toBe('');
  });

  it('returns to the same stored time for every boundary', () => {
    const cases = [
      ['entry', OPEN],
      ['entry', CLOSE],
      ['exit', OPEN],
      ['exit', CLOSE],
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
    expect(storedTime('entry', '10:00', odd)).toEqual({ ok: true, time: { anchor: 'open', offset_ms: 0 } });
    expect(storedTime('exit', '14:55', odd)).toEqual({ ok: true, time: { anchor: 'close', offset_ms: 5 * MINUTE } });
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
