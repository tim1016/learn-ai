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

  it('treats an empty field as empty, not as a mistake', () => {
    expect(checkDate('', { bounds: QQQ, sessions: SESSIONS })).toEqual({ date: '', problem: null });
  });

  it("refuses a day outside the bounds with the bound's own reason", () => {
    const held = { bounds: QQQ, sessions: null };

    expect(checkDate('2024-09-02', held).problem).toBe('QQQ is held from 2024-09-03.');
    expect(checkDate('2026-09-21', held).problem).toBe('QQQ is held through 2026-09-18.');
    expect(checkDate('2024-09-03', held).date).toBe('2024-09-03');
  });

  it('refuses a closed day, says which kind, and names the next session', () => {
    const trading = { bounds: UNBOUNDED, sessions: SESSIONS };

    expect(checkDate('2026-09-05', trading).problem).toBe('2026-09-05 is a weekend. The next session is 2026-09-08.');
    expect(checkDate('2026-09-07', trading).problem).toBe('2026-09-07 is a market holiday (Labor Day). The next session is 2026-09-08.');
    expect(checkDate('2027-03-01', trading).problem).toBe('The trading calendar runs from 2024-01-01 to 2026-12-31.');
  });

  it('leaves the day of the week alone when no calendar is given', () => {
    expect(checkDate('2026-09-05', { bounds: UNBOUNDED, sessions: null }).date).toBe('2026-09-05');
  });
});

describe('checkRange', () => {
  it('refuses a start after the end and keeps the end', () => {
    const { from, to } = checkRange('2026-09-10', '2026-09-01', { bounds: UNBOUNDED, sessions: SESSIONS });

    expect(from).toEqual({ date: '', problem: 'The start date is after the end date.' });
    expect(to.date).toBe('2026-09-01');
  });

  it('allows a single-day range', () => {
    const { from, to } = checkRange('2026-09-01', '2026-09-01', { bounds: UNBOUNDED, sessions: SESSIONS });

    expect([from.date, to.date]).toEqual(['2026-09-01', '2026-09-01']);
  });
});
