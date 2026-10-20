import { describe, expect, it } from 'vitest';

import { sessionOpenMs } from '../../shared/date-picker/testing/fake-trading-calendar';
import { missingParts, reasonGroups, sessionsWith, skippedSessions } from './data-check-view';
import type { DataProblem, SessionCheck } from './options-backtest.types';
import { sessionCheck } from './testing/fixtures';

const problem = (code: DataProblem['code'], message: string, lake_reason: string | null = null): DataProblem => ({ code, message, lake_reason });
const cannot = (day: string, ...problems: DataProblem[]): SessionCheck => sessionCheck(day, { verdict: 'cannot_be_tested', problems });

describe('the reasons sessions cannot be tested', () => {
  it('are grouped by code, each group counting its sessions in range order with the server’s message, keep every different message of one code once as served, list a session once for a code, and are none when every session can be tested', () => {
    const old = problem('older_than_the_data_plan', 'Older than the plan.');
    const fees = problem('no_published_fee_rates', 'No fee rates.');
    const sessions = [cannot('2026-09-14', old), cannot('2026-09-15', fees), cannot('2026-09-16', old, fees), sessionCheck('2026-09-17')];

    const groups = reasonGroups(sessions);

    expect.soft(groups.map((group) => group.code), 'are grouped by code, each group counting its sessions in range order with the server’s message (codes)').toEqual(['older_than_the_data_plan', 'no_published_fee_rates']);
    expect.soft(groups[0], 'are grouped by code, each group counting its sessions in range order with the server’s message (first group)').toMatchObject({ messages: ['Older than the plan.'], sessionOpensMs: [sessionOpenMs('2026-09-14'), sessionOpenMs('2026-09-16')] });
    expect.soft(groups[1], 'are grouped by code, each group counting its sessions in range order with the server’s message (second group)').toMatchObject({ messages: ['No fee rates.'], sessionOpensMs: [sessionOpenMs('2026-09-15'), sessionOpenMs('2026-09-16')] });

    const [failed] = reasonGroups([
      cannot('2026-09-14', problem('lake_holds_a_failure', 'Failed: A.', 'rate_limited')),
      cannot('2026-09-15', problem('lake_holds_a_failure', 'Failed: B.', 'rate_limited')),
      cannot('2026-09-16', problem('lake_holds_a_failure', 'Failed: A.', 'not_found')),
    ]);

    expect.soft(failed?.messages, 'keep every different message of one code, each once, as served (messages)').toEqual(['Failed: A.', 'Failed: B.']);
    expect.soft(failed?.lakeReasons, 'keep every different message of one code, each once, as served (lake reasons)').toEqual(['rate_limited', 'not_found']);

    const twice = cannot('2026-09-14', problem('stored_data_is_malformed', 'One.'), problem('stored_data_is_malformed', 'Two.'));

    expect.soft(reasonGroups([twice])[0]?.sessionOpensMs, 'list a session once for a code even when it gives the code twice').toEqual([sessionOpenMs('2026-09-14')]);
    expect.soft(reasonGroups([sessionCheck('2026-09-14'), sessionCheck('2026-09-15', { verdict: 'needs_download' })]), 'are none when every session can be tested').toEqual([]);
  });
});

describe('what a download must bring', () => {
  it('says each thing missing, and for contracts how many, or that it is not yet known, and is nothing for a session that is ready', () => {
    const known = sessionCheck('2026-09-14', {
      verdict: 'needs_download',
      missing: ['underlying_bars', 'listing', 'contracts'],
      contracts: { state: 'known', needed: 9, missing: 4 },
    });
    const unknown = sessionCheck('2026-09-15', { verdict: 'needs_download', missing: ['contracts'], contracts: { state: 'not_yet_known', needed: null, missing: null } });

    expect.soft(missingParts(known), 'says each thing missing, and for contracts how many (known)').toEqual([
      { code: 'underlying_bars', detail: null },
      { code: 'listing', detail: null },
      { code: 'contracts', detail: '4 of 9 missing' },
    ]);
    expect.soft(missingParts(unknown), 'says each thing missing, and for contracts that it is not yet known').toEqual([{ code: 'contracts', detail: 'not yet known' }]);
    expect.soft(missingParts(sessionCheck('2026-09-14')), 'is nothing for a session that is ready').toEqual([]);
  });
});

describe('the sessions with a verdict', () => {
  const sessions = [
    sessionCheck('2026-09-14'),
    sessionCheck('2026-09-15', { skip: 'window_does_not_fit' }),
    sessionCheck('2026-09-16', { verdict: 'needs_download' }),
    cannot('2026-09-17', problem('older_than_the_data_plan', 'Old.')),
    sessionCheck('2026-09-18', { verdict: 'needs_download', skip: 'underlying_did_not_trade' }),
  ];

  it('are those that have it, in range order, and for the skipped ones the ready sessions that carry a skip', () => {
    expect.soft(sessionsWith(sessions, 'needs_download').map((session) => session.session_open_ms), 'are those that have it, in range order').toEqual([sessionOpenMs('2026-09-16'), sessionOpenMs('2026-09-18')]);
    expect.soft(skippedSessions(sessions).map((session) => session.session_open_ms), 'are, for the skipped ones, the ready sessions that carry a skip').toEqual([sessionOpenMs('2026-09-15')]);
  });
});
