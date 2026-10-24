import type { IbkrChainSnapshot, IbkrOptionQuote, OptionRight } from '../../../../api/broker-models';
import {
  DELTA_GAP_GREEN_MAX,
  DELTA_GAP_YELLOW_MAX,
  absDiff,
  deltaAbsBand,
  type ToleranceBand,
} from '../../../../components/broker/format';
import type { QuantLibEngine } from '../../../../services/market-data.types';
import { etIsoDate } from '../../../../shared/date/et-midnight';
import { optionExpiryDate } from '../../../../shared/option-contract/option-expiry-date';
import type { ChainCrossCheck } from '../chain-presentation';

/*
 * The live cross-check (PRD #3490 R11): IBKR's delta for a contract against the engine's delta
 * for the same contract, valued with the inputs IBKR's own quote supplies.
 *
 * Nothing here is a new calculation. The engine's delta is the data plane's QuantLib pricer
 * (`QuantLibService.priceOption`, Python's), the gap is `absDiff` and the band is `deltaAbsBand`,
 * both exactly as the Chain (Live) page had them, and the engine is re-called under the same
 * rule (a spot, volatility, expiry or engine that moved, or a bid/ask midpoint that moved a
 * cent). What this module adds is the question every comparison must ask first: are both sides
 * present, current and like-for-like? A side that is not is `not_compared` with its reason, and
 * a missing input is never a gap of zero.
 */

/** A contract's quote is not compared once it is older than this at the snapshot's clock: the data plane's own limit for a quote a decision may use (`DECISION_DATA_MAX_AGE_MS`). */
export const QUOTE_MAX_AGE_MS = 5_000;

/** The stream cuts a snapshot every debounce window (half a second), so ten windows of silence is a stalled stream, not a quiet market. */
export const FRAME_STALL_MS = 5_000;

/** The engine is asked again when the bid/ask midpoint has moved this much, or when anything it is valued with has. */
const ENGINE_REPRICE_THRESHOLD_CENTS = 1;

/** What the engine is valued with for one contract. The midpoint is not a valuation input: it only decides when the engine is asked again. */
export interface EngineInput {
  readonly strike: number;
  readonly right: OptionRight;
  readonly mid: number;
  readonly spot: number;
  readonly iv: number;
  readonly expirationMs: number;
  readonly engine: QuantLibEngine;
}

export type NotCompared =
  | { readonly reason: 'stream_not_live' }
  | { readonly reason: 'no_ibkr_delta' }
  | { readonly reason: 'no_ibkr_iv' }
  | { readonly reason: 'no_two_sided_quote' }
  | { readonly reason: 'quote_stale'; readonly ageMs: number }
  | { readonly reason: 'no_spot' }
  | { readonly reason: 'expires_today' }
  | { readonly reason: 'engine_pending' }
  | { readonly reason: 'engine_failed'; readonly why: string };

export type NotComparedReason = NotCompared['reason'];

/** The order the reasons are listed in when a chain says why contracts were not compared. */
const REASON_ORDER: readonly NotComparedReason[] = [
  'stream_not_live',
  'no_ibkr_delta',
  'no_ibkr_iv',
  'no_two_sided_quote',
  'quote_stale',
  'no_spot',
  'expires_today',
  'engine_pending',
  'engine_failed',
];

function seconds(ms: number): string {
  const whole = Math.abs(ms) / 1000;
  return Number.isInteger(whole) ? `${whole} s` : `${whole.toFixed(1)} s`;
}

/** The reason in words, as a cell's hover and the chain's summary say it. */
export function notComparedWords(why: NotCompared): string {
  switch (why.reason) {
    case 'stream_not_live':
      return 'the stream is not delivering snapshots now, so the last one is stale';
    case 'no_ibkr_delta':
      return 'IBKR sent no delta for this contract';
    case 'no_ibkr_iv':
      return 'IBKR sent no usable implied volatility, and the engine is valued with it';
    case 'no_two_sided_quote':
      return 'the contract has no two-sided quote (a bid and an ask above zero)';
    case 'quote_stale':
      return `its quote is ${seconds(why.ageMs)} older than the snapshot, past the ${seconds(QUOTE_MAX_AGE_MS)} limit`;
    case 'no_spot':
      return 'IBKR sent no price for the underlying, and the engine is valued with it';
    case 'expires_today':
      return 'it expires today: with no whole day left the engine returns the intrinsic delta, which an intraday model delta cannot be held to';
    case 'engine_pending':
      return 'the engine has not answered yet';
    case 'engine_failed':
      return `the engine could not calculate it: ${why.why}`;
  }
}

/** The key a contract's engine read is filed under. */
export function contractKey(strike: number, right: OptionRight): string {
  return `${strike}:${right}`;
}

/** What the gates need to know besides the quote: whether the stream is delivering, and the snapshot the quote came in. */
export interface CrossCheckContext {
  readonly live: boolean;
  readonly snapshot: Pick<IbkrChainSnapshot, 'underlying_price' | 'as_of_ms' | 'expiry_ms'>;
  readonly engine: QuantLibEngine;
}

export type EngineReady =
  | { readonly kind: 'ready'; readonly input: EngineInput; readonly ibkrDelta: number }
  | { readonly kind: 'not_compared'; readonly why: NotCompared };

/** The midpoint of a two-sided quote, or null where the quote is not two-sided and above zero. */
function midOrNull(quote: IbkrOptionQuote): number | null {
  if (quote.bid === null || quote.ask === null) return null;
  const mid = (quote.bid + quote.ask) / 2;
  // A non-positive midpoint would trigger false reprices, and a quote the data plane has not stripped of its "no quote" sentinel is no quote.
  return mid > 0 ? mid : null;
}

/**
 * Whether a contract can be compared, and with what the engine is valued. The gates run in the
 * order a reader would ask them: is the stream live, did IBKR send a delta and a volatility, is
 * the quote two-sided and current, is there a spot, is there a day left to expiry.
 */
export function engineInputOf(quote: IbkrOptionQuote, context: CrossCheckContext): EngineReady {
  const refuse = (why: NotCompared): EngineReady => ({ kind: 'not_compared', why });
  const { snapshot } = context;
  if (!context.live) return refuse({ reason: 'stream_not_live' });
  if (quote.greeks_source === 'none' || quote.delta === null) return refuse({ reason: 'no_ibkr_delta' });
  if (quote.iv === null || quote.iv <= 0) return refuse({ reason: 'no_ibkr_iv' });
  const mid = midOrNull(quote);
  if (mid === null) return refuse({ reason: 'no_two_sided_quote' });
  const ageMs = snapshot.as_of_ms - quote.ts_ms;
  if (ageMs < 0 || ageMs > QUOTE_MAX_AGE_MS) return refuse({ reason: 'quote_stale', ageMs });
  const spot = snapshot.underlying_price;
  if (spot === null || spot <= 0) return refuse({ reason: 'no_spot' });
  if (optionExpiryDate(snapshot.expiry_ms) <= etIsoDate(snapshot.as_of_ms)) return refuse({ reason: 'expires_today' });
  return {
    kind: 'ready',
    ibkrDelta: quote.delta,
    input: { strike: quote.strike, right: quote.right, mid, spot, iv: quote.iv, expirationMs: snapshot.expiry_ms, engine: context.engine },
  };
}

/** Whether two inputs ask the engine the same question. */
export function sameEngineInput(left: EngineInput, right: EngineInput): boolean {
  return Math.abs(left.mid - right.mid) < ENGINE_REPRICE_THRESHOLD_CENTS / 100
    && left.spot === right.spot
    && left.iv === right.iv
    && left.expirationMs === right.expirationMs
    && left.engine === right.engine;
}

/** What the engine answered for one input. */
export type EngineAnswer =
  | { readonly kind: 'done'; readonly input: EngineInput; readonly delta: number; readonly gamma: number }
  | { readonly kind: 'failed'; readonly input: EngineInput; readonly why: string };

export type CrossCheckCell =
  | {
      readonly kind: 'compared';
      readonly ibkrDelta: number;
      readonly engineDelta: number;
      /** `|Δ IBKR − Δ engine|`, in delta units. */
      readonly gap: number;
      readonly band: ToleranceBand;
    }
  | { readonly kind: 'not_compared'; readonly why: NotCompared };

/** One contract's outcome: compared with its band, or not compared with the reason. */
export function crossCheckCell(quote: IbkrOptionQuote, context: CrossCheckContext, answer: EngineAnswer | undefined): CrossCheckCell {
  const ready = engineInputOf(quote, context);
  if (ready.kind === 'not_compared') return ready;
  // An answer for a different question is no answer: the engine is asked again and the cell waits.
  if (answer === undefined || !sameEngineInput(answer.input, ready.input)) return { kind: 'not_compared', why: { reason: 'engine_pending' } };
  if (answer.kind === 'failed') return { kind: 'not_compared', why: { reason: 'engine_failed', why: answer.why } };
  const gap = absDiff(ready.ibkrDelta, answer.delta);
  const band = deltaAbsBand(gap);
  // Both deltas are numbers here, so a null gap means the engine's was not one: the cell refuses rather than show a figure it cannot judge.
  if (gap === null || band === null) return { kind: 'not_compared', why: { reason: 'engine_failed', why: 'it returned no delta' } };
  return { kind: 'compared', ibkrDelta: ready.ibkrDelta, engineDelta: answer.delta, gap, band };
}

/** The reason in a few words, for the count of contracts left out. */
const REASON_SHORT: Readonly<Record<NotComparedReason, string>> = {
  stream_not_live: 'stream not live',
  no_ibkr_delta: 'no IBKR delta',
  no_ibkr_iv: 'no usable IBKR volatility',
  no_two_sided_quote: 'no two-sided quote',
  quote_stale: `quote older than ${seconds(QUOTE_MAX_AGE_MS)}`,
  no_spot: 'no underlying price',
  expires_today: 'expire today',
  engine_pending: 'waiting for the engine',
  engine_failed: 'engine failed',
};

/** How the chain says what it compared: how many, and why the others were not. */
export function crossCheckOutcome(cells: readonly CrossCheckCell[]): string | null {
  if (cells.length === 0) return null;
  const compared = cells.filter((cell) => cell.kind === 'compared').length;
  const counts = new Map<NotComparedReason, number>();
  for (const cell of cells) if (cell.kind === 'not_compared') counts.set(cell.why.reason, (counts.get(cell.why.reason) ?? 0) + 1);
  const head = `Compared ${compared} of ${cells.length} contracts.`;
  if (counts.size === 0) return head;
  const parts = REASON_ORDER.filter((reason) => counts.has(reason)).map((reason) => `${counts.get(reason)} ${REASON_SHORT[reason]}`);
  return `${head} Not compared: ${parts.join('; ')}.`;
}


/**
 * The statement printed above the table: which quantity the columns compare, in what units, what
 * each side is, what both were valued with, which instants they are as of, and the tolerance.
 */
export function crossCheckStatement(engine: QuantLibEngine, outcome: string | null): ChainCrossCheck {
  return {
    title: 'What the Δ Engine and |ΔΔ| columns compare',
    statements: [
      { label: 'Quantity', text: 'Delta (Δ), the change in an option\'s price for a $1 move in its underlying. It has no unit: a number from 0 to 1 for a call and from −1 to 0 for a put.' },
      { label: 'Δ IBKR', text: 'The delta IBKR computed for the contract: from its model Greeks, or from its bid, ask or last Greeks when it sent no model Greeks. It is as of the contract\'s own quote, whose receipt time is the hover of the figure.' },
      { label: 'Δ Engine', text: `The delta the data plane's QuantLib pricer returns for the same contract (engine ${engine}, European exercise), asked once per contract.` },
      { label: '|ΔΔ|', text: `The distance between the two, |Δ IBKR − Δ Engine|, in delta units. Green up to ${DELTA_GAP_GREEN_MAX}, yellow up to ${DELTA_GAP_YELLOW_MAX}, red above. Where it is not shown the cell says unavailable and why: a missing or stale input is never a gap of zero.` },
      { label: 'Valued with', text: 'The strike and right of the contract; the underlying\'s price as IBKR gave it when the snapshot was cut; the contract\'s own IBKR implied volatility (annualised); the expiry at 16:00 New York time on its date; the data plane\'s current New York date as the valuation date, in whole days; the data plane\'s default risk-free rate; a dividend yield of 0.' },
      { label: 'As of', text: `Δ IBKR and the volatility: each contract's own quote. The underlying's price: the snapshot clock (its own receipt time is not on the feed). A contract whose quote is more than ${seconds(QUOTE_MAX_AGE_MS)} older than the snapshot is not compared, and neither is any contract while no snapshot has arrived for ${seconds(FRAME_STALL_MS)}.` },
      { label: 'Not compared', text: 'On the expiry date, because the engine counts whole days and returns the intrinsic delta (1, −1 or 0) at none; where the contract has no two-sided quote; and where IBKR sent no delta, volatility or underlying price.' },
    ],
    outcome,
  };
}
