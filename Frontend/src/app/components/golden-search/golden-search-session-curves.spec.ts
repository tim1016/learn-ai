import { describe, expect, it } from 'vitest';

import { etMidnightMs } from '../../shared/date/et-midnight';
import { onOneAxis, recordedSpan } from './golden-search-session-curves';

const [JAN_2, JAN_3, JAN_6, JAN_7] = ['2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07'].map((day) => etMidnightMs(day));

describe('onOneAxis', () => {
  it('places each curve from its own first session and leaves a gap where it scored nothing', () => {
    const sessions = [JAN_2, JAN_3, JAN_6, JAN_7];

    const axis = onOneAxis([
      { sessions, curve: { first_session: 0, cumulative_return: [0.01, null, 0.03], drawdown: [0, null, -0.02] } },
      { sessions, curve: { first_session: 1, cumulative_return: [0.06, 0.05, 0.04], drawdown: null } },
    ]);

    expect(axis.dates).toEqual(['2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07']);
    expect(axis.lines[0]).toEqual({ returns: [0.01, null, 0.03, null], falls: [0, null, -0.02, null] });
    // A curve with no fall recorded has none at any session, never a zero.
    expect(axis.lines[1]).toEqual({ returns: [null, 0.06, 0.05, 0.04], falls: [null, null, null, null] });
  });

  it('gives the axis a session only when a drawn curve scored it', () => {
    // Read beside a run that scored Jan 3, this one carries a gap there; drawn alone, the day is no part of its axis.
    const axis = onOneAxis([{ sessions: [JAN_2, JAN_3, JAN_6], curve: { first_session: 0, cumulative_return: [0.01, null, 0.03], drawdown: [0, null, 0] } }]);

    expect(axis.dates).toEqual(['2025-01-02', '2025-01-06']);
    expect(axis.lines[0].returns).toEqual([0.01, 0.03]);
  });

  it('keeps a session the caller names on the axis though no curve scored it', () => {
    // A walk-forward fold that did not run: Jan 3 and Jan 6 stand empty between two folds that did.
    const sessions = [JAN_2, JAN_3, JAN_6, JAN_7];
    const axis = onOneAxis(
      [
        { sessions, curve: { first_session: 0, cumulative_return: [0.01], drawdown: [0] } },
        { sessions, curve: { first_session: 3, cumulative_return: [0.02], drawdown: [0] } },
      ],
      sessions,
    );

    expect(axis.dates).toEqual(['2025-01-02', '2025-01-03', '2025-01-06', '2025-01-07']);
    expect(axis.lines.map((line) => line.returns)).toEqual([
      [0.01, null, null, null],
      [null, null, null, 0.02],
    ]);
  });

  it('joins curves of separate reads by instant, whatever index each read gave the session', () => {
    const axis = onOneAxis([
      { sessions: [JAN_6, JAN_7], curve: { first_session: 0, cumulative_return: [0.02, 0.03], drawdown: [0, 0] } },
      { sessions: [JAN_2, JAN_3, JAN_6], curve: { first_session: 1, cumulative_return: [0.1, 0.2], drawdown: [0, 0] } },
    ]);

    expect(axis.dates).toEqual(['2025-01-03', '2025-01-06', '2025-01-07']);
    expect(axis.lines.map((line) => line.returns)).toEqual([
      [null, 0.02, 0.03],
      [0.1, 0.2, null],
    ]);
  });
});

describe('recordedSpan', () => {
  it('is the first and last sessions a line recorded, and nothing for a line that recorded none', () => {
    expect(recordedSpan([null, 0.1, null, 0.2, null])).toEqual([1, 3]);
    expect(recordedSpan([null, null])).toBeNull();
  });
});
