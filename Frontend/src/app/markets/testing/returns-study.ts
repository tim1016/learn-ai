import type { DayReturns, KindDistribution, ReturnDistributionStudy } from '../returns/returns-distribution.service';

/*
 * A two-day Return Distribution study, as the service maps it, for the specs
 * of the study and of the tabs around it. Pure data: it imports no test runner.
 */

export const DAY_MS = (july: number) => Date.UTC(2024, 6, july, 13, 30);

export function kindFixture(kind: KindDistribution['kind'], nDays: number): KindDistribution {
  return {
    kind,
    bins: [
      { lowerPct: 0, upperPct: 0.5, count: nDays, isEdge: false },
      { lowerPct: 0.5, upperPct: 1, count: 0, isEdge: false },
    ],
    normalExpectedCounts: [nDays * 0.6, nDays * 0.1],
    stats: {
      nDays,
      meanPct: 0.05,
      stdPct: 0.9,
      annualizedVolPct: 14.3,
      skewness: -0.2,
      excessKurtosis: 1.5,
      var95Pct: -1.4,
      cvar95Pct: -2.0,
      bestDay: { sessionOpenMsUtc: DAY_MS(2), valuePct: 2.5 },
      worstDay: { sessionOpenMsUtc: DAY_MS(3), valuePct: -1.8 },
    },
  };
}

export const DAYS: DayReturns[] = [
  {
    sessionOpenMsUtc: DAY_MS(2),
    closeToClosePct: 0.25,
    sessionPct: 0.31,
    overnightPct: -0.06,
    preMarketPct: 0.02,
    morningPct: 0.19,
    afternoonPct: 0.12,
    afterHoursPct: null,
    volume: 4_500_000,
    binIndices: { close_to_close: 0, session: 0, overnight: 1 },
  },
  {
    sessionOpenMsUtc: DAY_MS(3),
    closeToClosePct: 0.4,
    sessionPct: 0.2,
    overnightPct: 0.2,
    preMarketPct: null,
    morningPct: 0.0,
    afternoonPct: 0.2,
    afterHoursPct: 0.01,
    volume: 3_000_000,
    binIndices: { close_to_close: 0, session: 0, overnight: 1 },
  },
];

export const STUDY: ReturnDistributionStudy = {
  adjustment: 'split_and_dividend',
  warnings: [],
  capture: {
    status: 'complete',
    fetchedArtifactCount: 480,
    detail: null,
  },
  coverage: {
    requestedSessions: 2,
    returnedSessions: 2,
    missingSessions: 0,
    excludedSessions: 0,
    firstSessionOpenMsUtc: DAY_MS(2),
    lastSessionOpenMsUtc: DAY_MS(3),
  },
  kinds: [kindFixture('close_to_close', 2), kindFixture('session', 2), kindFixture('overnight', 2)],
  days: DAYS,
};
