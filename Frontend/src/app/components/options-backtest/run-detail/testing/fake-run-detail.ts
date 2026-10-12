import type { Mock, vi as Vi } from 'vitest';

import { RunDetailRefusedError, type RunDetailService } from '../run-detail.service';
import type { CsvDownload, RunComparison, SessionDetail, TradeQuery, TradesPage } from '../run-detail.types';
import { tradesPage } from './detail-fixtures';

/** The `vi` of the spec that calls: a shared helper never imports vitest itself. */
type Spy = Pick<typeof Vi, 'fn'>;

export interface FakeRunDetail {
  trades: Mock<(runId: number, query?: TradeQuery) => Promise<TradesPage>>;
  session: Mock<(runId: number, sessionOpenMs: number) => Promise<SessionDetail>>;
  tradesCsv: Mock<(runId: number) => Promise<CsvDownload>>;
  compare: Mock<(a: number, b: number) => Promise<RunComparison>>;
}

/**
 * A stand-in for `RunDetailService`. Unless a spec says otherwise a run has no trades, a session is not found and a
 * comparison is refused, so a spec that is about something else never meets the closer look by accident.
 */
export function fakeRunDetail(spy: Spy, changes: Partial<FakeRunDetail> = {}): FakeRunDetail {
  return {
    trades: spy.fn(async (runId: number) => tradesPage({ run_id: runId, total: 0, trades: [] })),
    session: spy.fn(async (runId: number, sessionOpenMs: number) => {
      throw new RunDetailRefusedError('OPTIONS_BACKTEST_SESSION_NOT_IN_RUN', `Run ${runId} did not test a session that opened at ${sessionOpenMs}.`, 404);
    }),
    tradesCsv: spy.fn(async () => ({ blob: new Blob(['unix_ts\r\n']), filename: 'options-backtest-run-7-trades.csv' })),
    compare: spy.fn(async (a: number, b: number) => {
      throw new RunDetailRefusedError('OPTIONS_BACKTEST_RUN_NOT_FOUND', `No run has the id ${a} (run a), or ${b}.`, 404);
    }),
    ...changes,
  };
}

/** What `RunDetailService` is provided as when a spec does not care: no trades, no sessions, no comparison, and no mock to count. */
export function inertRunDetail(): Pick<RunDetailService, 'trades' | 'session' | 'tradesCsv' | 'compare'> {
  return {
    trades: async (runId) => tradesPage({ run_id: runId, total: 0, trades: [] }),
    session: async (runId, sessionOpenMs) => {
      throw new RunDetailRefusedError('OPTIONS_BACKTEST_SESSION_NOT_IN_RUN', `Run ${runId} did not test a session that opened at ${sessionOpenMs}.`, 404);
    },
    tradesCsv: async () => ({ blob: new Blob(['unix_ts\r\n']), filename: 'options-backtest-run-7-trades.csv' }),
    compare: async (a) => {
      throw new RunDetailRefusedError('OPTIONS_BACKTEST_RUN_NOT_FOUND', `No run has the id ${a} (run a).`, 404);
    },
  };
}
