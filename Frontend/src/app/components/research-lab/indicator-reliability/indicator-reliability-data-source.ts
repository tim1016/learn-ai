import type { components } from '../../../api/broker.types';
import { refusalBody } from '../../../shared/errors/refusal-body';

/** Where an indicator reliability study's bars came from: the lake input, the admitted bytes, what admission did (#3523). */
export type ReliabilityDataSource = components['schemas']['ReliabilityDataSource'];

type WarmupCause = components['schemas']['ReliabilityWarmup']['cause'];

const ADJUSTMENT_WORDS: Record<ReliabilityDataSource['price_adjustment_mode'], string> = {
  polygon_split_adjusted: 'split-adjusted',
  raw: 'unadjusted',
};

const CAUSE_WORDS: Record<WarmupCause, string> = {
  none: 'no shortfall',
  provider_history_floor: "the provider's history does not reach back further",
  symbol_history_starts: 'the symbol has no bars before its first listed session',
  sparse_session_bars: 'the lead-in sessions hold fewer bars than a fully traded session',
};

export interface ReliabilitySourceLine {
  /** What the bars are: vendor, adjustment, session, size. */
  method: string;
  /** What admission did for this run, in distinct sessions. */
  freshness: string;
  /** Present when the warmup lead-in was shorter than the policy asks; the indicator's first values lean on it. */
  notice: string | null;
}

const sessions = (n: number): string => `${n} session${n === 1 ? '' : 's'}`;

/** The words a data-source line shows for a study's lake source: counts are sessions, never artifacts. */
export function describeReliabilitySource(source: ReliabilityDataSource): ReliabilitySourceLine {
  const size = source.bar_minutes === 60 ? '1-hour bars' : `${source.bar_minutes}-min bars`;
  const hours = source.session_policy === 'regular' ? 'regular hours' : 'extended hours';
  const method = `Polygon · ${ADJUSTMENT_WORDS[source.price_adjustment_mode]} · ${hours} · ${size}`;

  const coverage = source.coverage;
  let freshness = 'not admitted';
  if (coverage) {
    const group = (g: typeof coverage.window): string =>
      `${sessions(g.expected_sessions)}: ${g.reused} reused · ${g.added} added · ${g.missing} missing · ${g.failed} failed`;
    freshness = `window ${group(coverage.window)}`;
    if (coverage.warmup.expected_sessions > 0) freshness += ` · warmup ${group(coverage.warmup)}`;
  }

  const warmup = source.warmup;
  const notice = warmup?.shortened
    ? `Warmup is ${warmup.shortfall_bars} bars short of the ${warmup.requested_bars} the policy asks for ` +
      `(${CAUSE_WORDS[warmup.cause]}); the indicator's first values may be less settled.`
    : null;
  return { method, freshness, notice };
}

/** The data source a refusal carries, so a refused run still shows what admission reached. */
export function refusalDataSource(error: unknown): ReliabilityDataSource | null {
  const source = refusalBody(error)?.['data_source'];
  // The cast is the wire boundary: the body is FastAPI's JSON and the block's shape is the generated contract's.
  return typeof source === 'object' && source !== null ? (source as ReliabilityDataSource) : null;
}
