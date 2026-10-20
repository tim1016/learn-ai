import { describe, expect, it } from 'vitest';

import { UNBOUNDED, heldBounds } from './date-bounds';
import { checkDate, checkRange } from './date-check';
import { fakeSessions } from './testing/fake-trading-calendar';

const SESSIONS = fakeSessions('2024-01-01', '2026-12-31', { '2026-09-07': 'Labor Day' });
const QQQ = heldBounds('QQQ', '2024-09-03', '2026-09-18');

describe('checkDate', () => {
  it('takes a whole YYYY-MM-DD date and nothing else', () => {
    const any = { bounds: UNBOUNDED, sessions: null };

    expect(checkDate('2022-11-01', any)).toEqual({ date: '2022-11-01', problem: null });
    for (const text of ['2022-11', '11/01/2022', '20221101', '2022-13-01', '2023-02-29', '0022-11-01']) {
      expect(checkDate(text, any)).toEqual({ date: '', problem: 'Enter a date as YYYY-MM-DD.' });
    }
  });

  it('judges a typed date against the bounds and the calendar it is given', () => {
    expect.soft(
      checkDate('', { bounds: QQQ, sessions: SESSIONS }),
      'treats an empty field as empty, not as a mistake',
    ).toEqual({ date: '', problem: null });

    const held = { bounds: QQQ, sessions: null };

    expect.soft(checkDate('2024-09-02', held).problem, "refuses a day outside the bounds with the bound's own reason: before the first held day").toBe('QQQ is held from 2024-09-03.');
    expect.soft(checkDate('2026-09-21', held).problem, "refuses a day outside the bounds with the bound's own reason: after the last held day").toBe('QQQ is held through 2026-09-18.');
    expect.soft(checkDate('2024-09-03', held).date, "refuses a day outside the bounds with the bound's own reason: the first held day is taken").toBe('2024-09-03');

    const trading = { bounds: UNBOUNDED, sessions: SESSIONS };

    expect.soft(checkDate('2026-09-05', trading).problem, 'refuses a closed day, says which kind, and names the next session: a weekend').toBe('2026-09-05 is a weekend. The next session is 2026-09-08.');
    expect.soft(checkDate('2026-09-07', trading).problem, 'refuses a closed day, says which kind, and names the next session: a market holiday').toBe('2026-09-07 is a market holiday (Labor Day). The next session is 2026-09-08.');
    expect.soft(checkDate('2027-03-01', trading).problem, 'refuses a closed day, says which kind, and names the next session: a day past the calendar').toBe('The trading calendar runs from 2024-01-01 to 2026-12-31.');

    expect.soft(
      checkDate('2026-09-05', { bounds: UNBOUNDED, sessions: null }).date,
      'leaves the day of the week alone when no calendar is given',
    ).toBe('2026-09-05');
  });
});

describe('checkRange', () => {
  it('refuses a start after the end and keeps the end, and allows a single-day range', () => {
    const { from, to } = checkRange('2026-09-10', '2026-09-01', { bounds: UNBOUNDED, sessions: SESSIONS });

    expect.soft(from, 'refuses a start after the end').toEqual({ date: '', problem: 'The start date is after the end date.' });
    expect.soft(to.date, 'keeps the end when the start is after it').toBe('2026-09-01');

    const single = checkRange('2026-09-01', '2026-09-01', { bounds: UNBOUNDED, sessions: SESSIONS });

    expect.soft([single.from.date, single.to.date], 'allows a single-day range').toEqual(['2026-09-01', '2026-09-01']);
  });
});
