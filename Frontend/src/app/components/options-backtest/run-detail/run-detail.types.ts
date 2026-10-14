import type { components } from '../../../api/broker.types';

type Schemas = components['schemas'];

/** A page of a run's leg trades, and one trade of it. */
export type TradesPage = Schemas['OptionsBacktestTradesResponse'];
export type LegTrade = Schemas['OptionsBacktestTradeResponse'];
export type TradeContract = Schemas['OptionsBacktestContractResponse'];

/** One session of a run for the one-day timeline, and what it holds. */
export type SessionDetail = Schemas['OptionsBacktestSessionResponse'];
export type SessionEvent = Schemas['OptionsBacktestEventResponse'];
export type SessionSeries = Schemas['OptionsBacktestSeriesResponse'];
export type SeriesPoint = Schemas['OptionsBacktestSeriesPointResponse'];
export type SessionLevel = Schemas['OptionsBacktestLevelResponse'];
export type SessionFill = Schemas['OptionsBacktestFillResponse'];
export type SessionMark = Schemas['OptionsBacktestMarkResponse'];
export type SeriesReason = NonNullable<SessionSeries['reason']>;
export type LevelKind = SessionLevel['kind'];

/** Two runs side by side. */
export type RunComparison = Schemas['OptionsBacktestComparisonResponse'];
export type ComparedRun = Schemas['OptionsBacktestComparedRunResponse'];

/** Which trades to read: only one session's, only one leg's, and which page. */
export interface TradeQuery {
  readonly sessionOpenMs?: number | null;
  readonly legId?: string | null;
  readonly limit?: number;
  readonly offset?: number;
}

/** The export: the bytes exactly as the server sent them, and the name it gave them (null when it gave none). */
export interface CsvDownload {
  readonly blob: Blob;
  readonly filename: string | null;
}
