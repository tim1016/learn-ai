import { describe, expect, it } from 'vitest';

import { fleetPage, qualification, runningBot } from './testing/fixtures';
import {
  MAX_RUNNING_PAGES,
  allowedRangeText,
  readRunningFleet,
  runningBotsFor,
  versionsBySymbol,
  type FleetGap,
} from './signal-strategy-evidence';

const UNREADABLE: FleetGap = {
  broker: 'alpaca',
  clerk_id: 'clrk_live',
  account_id: null,
  strategy_instance_id: null,
  reason: 'The account could not be reached.',
  reason_code: 'clerk_unreachable',
};

describe('versionsBySymbol', () => {
  it('groups one strategy’s versions by the symbol each approves, newest first, and counts each status per symbol', () => {
    const groups = versionsBySymbol('ema_crossover_signal', [
      qualification({ id: 'spy-old', symbol: 'SPY', status: 'stale', created_at_ms: 1 }),
      qualification({ id: 'spy-new', symbol: 'SPY', status: 'ready', created_at_ms: 2 }),
      qualification({ id: 'qqq-1', symbol: 'QQQ', status: 'revoked' }),
      qualification({ id: 'qqq-2', symbol: 'QQQ', status: 'unverifiable' }),
      qualification({ id: 'other', program_key: 'sma_crossover', symbol: 'AAPL' }),
    ]);

    expect(groups.map((group) => group.symbol)).toEqual(['QQQ', 'SPY']);
    expect(groups[1].versions.map((version) => version.id)).toEqual(['spy-new', 'spy-old']);
    expect(groups[1].statusCounts).toEqual([
      { status: 'ready', count: 1 },
      { status: 'stale', count: 1 },
    ]);
    expect(groups[0].statusCounts).toEqual([
      { status: 'revoked', count: 1 },
      { status: 'unverifiable', count: 1 },
    ]);
  });
});

describe('readRunningFleet', () => {
  it('is complete when every page is read and no account was unreadable', async () => {
    const read = await readRunningFleet(async () => fleetPage());

    expect(read.complete).toBe(true);
    expect(read.rows).toHaveLength(1);
  });

  it('follows every page and reports the oldest page’s as-of', async () => {
    const pages = [
      fleetPage({ rows: [runningBot({ strategy_instance_id: 'a' })], total: 2, page_size: 1, observed_at_ms: 500 }),
      fleetPage({ rows: [runningBot({ strategy_instance_id: 'b' })], total: 2, page: 2, page_size: 1, observed_at_ms: 400 }),
    ];

    const read = await readRunningFleet(async (page) => pages[page - 1]);

    expect(read.rows.map((row) => row.strategy_instance_id)).toEqual(['a', 'b']);
    expect(read.observedAtMs).toBe(400);
    expect(read.complete).toBe(true);
  });

  it.each([
    ['an account the read could not see', fleetPage({ gaps: [UNREADABLE] })],
    ['a fleet longer than the pages it will follow', fleetPage({ total: (MAX_RUNNING_PAGES + 1) * 25, page_size: 25 })],
    ['a total that no longer matches the rows', fleetPage({ total: 3 })],
  ])('is incomplete for %s', async (_name, answer) => {
    const read = await readRunningFleet(async () => answer);

    expect(read.complete).toBe(false);
  });

  it('counts a bot once when the fleet shifted between two pages, and says the read is incomplete', async () => {
    // A bot started after page 1 was read pushed "b" onto page 2 as well: it is listed twice, and another bot is missed.
    const pages = [
      fleetPage({ rows: [runningBot({ strategy_instance_id: 'a' }), runningBot({ strategy_instance_id: 'b' })], total: 4, page_size: 2 }),
      fleetPage({ rows: [runningBot({ strategy_instance_id: 'b' }), runningBot({ strategy_instance_id: 'c' })], total: 4, page: 2, page_size: 2 }),
    ];

    const read = await readRunningFleet(async (page) => pages[page - 1]);

    expect(read.rows.map((row) => row.strategy_instance_id)).toEqual(['a', 'b', 'c']);
    expect(read.complete).toBe(false);
  });

  it('is incomplete when the pages disagree about the total, though the rows add up', async () => {
    const pages = [
      fleetPage({ rows: [runningBot({ strategy_instance_id: 'a' })], total: 2, page_size: 1 }),
      fleetPage({ rows: [runningBot({ strategy_instance_id: 'b' })], total: 3, page: 2, page_size: 1 }),
    ];

    const read = await readRunningFleet(async (page) => pages[page - 1]);

    expect(read.rows).toHaveLength(2);
    expect(read.complete).toBe(false);
  });

  it('rejects when a page cannot be read, rather than answering with the pages it has', async () => {
    const pages = [fleetPage({ total: 2, page_size: 1 })];

    await expect(
      readRunningFleet(async (page) => {
        if (page > 1) throw new Error('second page failed');
        return pages[0];
      }),
    ).rejects.toThrow('second page failed');
  });
});

describe('runningBotsFor', () => {
  const complete = { observedAtMs: 10, rows: [runningBot({ world: 'paper' }), runningBot({ world: 'dry_run', strategy_instance_id: 'dry' })], gaps: [], complete: true };

  it('says unknown when there is no read to go by', () => {
    expect(runningBotsFor('ema_crossover_signal', null).state).toBe('unknown');
  });

  it('says none only for a complete read that found none', () => {
    expect(runningBotsFor('sma_crossover', complete).state).toBe('none');
    expect(runningBotsFor('sma_crossover', { ...complete, complete: false, gaps: [UNREADABLE] }).state).toBe('unknown');
  });

  it('counts a strategy’s bots by world in History’s own order, and calls an incomplete count a lower bound', () => {
    const answer = runningBotsFor('ema_crossover_signal', complete);
    const partial = runningBotsFor('ema_crossover_signal', { ...complete, complete: false, gaps: [UNREADABLE] });

    expect(answer.worlds.map((world) => `${world.label} ${world.count}`)).toEqual(['Paper 1', 'Dry Run 1']);
    expect(answer.atLeast).toBe(false);
    expect(partial.atLeast).toBe(true);
  });
});

describe('allowedRangeText', () => {
  it.each([
    [{ type: 'integer', minimum: 2, maximum: 500 }, '≥ 2 and ≤ 500'],
    [{ type: 'number', exclusiveMinimum: 0, exclusiveMaximum: 100 }, '> 0 and < 100'],
    [{ type: 'number', minimum: 0 }, '≥ 0'],
    [{ type: 'number' }, 'no limit declared'],
    [{ type: 'string', maxLength: 20 }, 'text up to 20 characters'],
  ])('words %j as %s', (property, expected) => {
    expect(allowedRangeText(property)).toBe(expected);
  });
});
