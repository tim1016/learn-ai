import type { IbkrChainSnapshot, IbkrOptionQuote } from '../../../../api/broker-models';
import { optionExpiryDate } from '../../../../shared/option-contract/option-expiry-date';
import type { QuantLibEngine } from '../../../../services/market-data.types';
import {
  absent,
  type ChainColumns,
  type ChainContractCell,
  type ChainFact,
  type ChainField,
  type ChainPresentation,
  type ChainRow,
  type ChainSideCell,
} from '../chain-presentation';
import {
  contractKey,
  crossCheckCell,
  crossCheckOutcome,
  crossCheckStatement,
  notComparedWords,
  type CrossCheckCell,
  type CrossCheckContext,
  type EngineAnswer,
} from './live-cross-check';
import type { StatusLine } from './live-feed-status';

/*
 * Now · Live IBKR, mapped into the one chain presentation. The columns are the Chain (Live)
 * page's, mirrored about the strike as the grid draws every chain: bid, ask, implied volatility
 * and delta from IBKR, then the engine's delta and the gap between the two. A figure IBKR has
 * not sent is absent with that reason, and a comparison that was not made says "unavailable"
 * and why; nothing is filled from another source.
 */

const COLUMNS: ChainColumns = {
  calls: ['deltaGap', 'engineDelta', 'delta', 'iv', 'bid', 'ask'],
  puts: ['bid', 'ask', 'iv', 'delta', 'engineDelta', 'deltaGap'],
  detail: 'compact',
  headers: {
    iv: { label: 'IV', title: 'IBKR\'s implied volatility for the contract, annualised. The engine is valued with it.' },
    delta: { label: 'Δ IBKR', title: 'IBKR\'s delta for the contract, as of its own quote.' },
    engineDelta: { label: 'Δ Engine', title: 'The data plane\'s QuantLib delta for the contract, valued with IBKR\'s volatility and the underlying\'s price in the snapshot.' },
    deltaGap: { label: '|ΔΔ|', title: '|Δ IBKR − Δ Engine|, in delta units. Unavailable, never zero, when an input is missing or stale.' },
  },
};

export interface LiveChainInputs {
  readonly symbol: string;
  /** The expiry on screen; null while none is picked. */
  readonly expiryMs: number | null;
  readonly snapshot: IbkrChainSnapshot | null;
  /** Whether the data plane's IBKR session can serve a chain, so a comparison can be made at all. */
  readonly serving: boolean;
  /** Whether the stream is delivering snapshots now. */
  readonly live: boolean;
  readonly engine: QuantLibEngine;
  readonly answers: ReadonlyMap<string, EngineAnswer>;
  readonly connection: StatusLine;
  readonly entitlement: StatusLine;
  readonly subscription: StatusLine;
}

const price = (value: number | null, atMs: number): ChainField =>
  value === null ? absent('not_sent_yet') : { kind: 'value', text: value.toFixed(2), atMs };

const figure = (value: number | null, digits: number, atMs: number): ChainField =>
  value === null ? absent('not_sent_yet') : { kind: 'value', text: value.toFixed(digits), atMs };

const percent = (value: number | null, atMs: number): ChainField =>
  value === null ? absent('not_sent_yet') : { kind: 'value', text: `${(value * 100).toFixed(2)}%`, atMs };

const NOT_A_FEED_FIELD = absent('not_on_feed');

function comparisonFields(cell: CrossCheckCell): Pick<ChainContractCell['fields'], 'engineDelta' | 'deltaGap'> {
  if (cell.kind === 'not_compared') {
    const unavailable = absent('not_compared', notComparedWords(cell.why));
    return { engineDelta: { ...unavailable, shown: 'unavailable' }, deltaGap: { ...unavailable, shown: 'unavailable' } };
  }
  return {
    engineDelta: { kind: 'value', text: cell.engineDelta.toFixed(3), atMs: null },
    deltaGap: { kind: 'value', text: cell.gap.toFixed(4), atMs: null, band: cell.band },
  };
}

function contractCell(quote: IbkrOptionQuote, cell: CrossCheckCell): ChainContractCell {
  const at = quote.ts_ms;
  return {
    kind: 'contract',
    symbol: quote.symbol,
    standard: null,
    fields: {
      bid: price(quote.bid, at),
      ask: price(quote.ask, at),
      last: price(quote.last, at),
      spread: NOT_A_FEED_FIELD,
      iv: percent(quote.iv, at),
      delta: figure(quote.delta, 3, at),
      gamma: figure(quote.gamma, 3, at),
      theta: figure(quote.theta, 3, at),
      vega: figure(quote.vega, 3, at),
      volume: NOT_A_FEED_FIELD,
      openInterest: NOT_A_FEED_FIELD,
      ...comparisonFields(cell),
    },
  };
}

interface Sides {
  call: IbkrOptionQuote | null;
  put: IbkrOptionQuote | null;
}

/** The strike nearest the spot, or null where there is no spot. */
function moneyStrike(strikes: readonly number[], spot: number | null): number | null {
  if (spot === null || spot <= 0 || strikes.length === 0) return null;
  return strikes.reduce((nearest, strike) => (Math.abs(strike - spot) < Math.abs(nearest - spot) ? strike : nearest));
}

/** The rows of a snapshot, and every contract's outcome, for the outcome line. */
function rowsOf(snapshot: IbkrChainSnapshot, context: CrossCheckContext, answers: ReadonlyMap<string, EngineAnswer>): { rows: ChainRow[]; cells: CrossCheckCell[] } {
  const byStrike = new Map<number, Sides>();
  for (const quote of snapshot.quotes) {
    const sides = byStrike.get(quote.strike) ?? { call: null, put: null };
    if (quote.right === 'C') sides.call = quote;
    else sides.put = quote;
    byStrike.set(quote.strike, sides);
  }
  const ordered = [...byStrike.entries()].sort(([a], [b]) => a - b);
  const spot = snapshot.underlying_price;
  const atm = moneyStrike(ordered.map(([strike]) => strike), spot);
  const cells: CrossCheckCell[] = [];
  const side = (quote: IbkrOptionQuote | null): ChainSideCell => {
    if (quote === null) return { kind: 'gap', words: 'No contract at this strike' };
    const outcome = crossCheckCell(quote, context, answers.get(contractKey(quote.strike, quote.right)));
    cells.push(outcome);
    return contractCell(quote, outcome);
  };
  const rows = ordered.map<ChainRow>(([strike, sides]) => {
    const isMoney = strike === atm;
    return {
      key: String(strike),
      strike: strike.toFixed(2),
      atm: isMoney,
      mark: isMoney && spot !== null ? `ATM ${spot.toFixed(2)}` : null,
      itm: spot !== null && spot > 0 && !isMoney ? (strike < spot ? 'call' : 'put') : null,
      centre: null,
      call: side(sides.call),
      put: side(sides.put),
    };
  });
  return { rows, cells };
}

function underlyingText(symbol: string, spot: number | null): string {
  return spot === null || spot <= 0
    ? `${symbol} —: IBKR has sent no price for it`
    : `${symbol} ${spot.toFixed(2)}, the underlying's price when the snapshot was cut`;
}

function facts(inputs: LiveChainInputs): readonly ChainFact[] {
  const { snapshot } = inputs;
  const line = (label: string, status: StatusLine): ChainFact => ({ label, value: { kind: 'text', text: status.text }, ...(status.tone === undefined ? {} : { tone: status.tone }) });
  return [
    { label: 'Source', value: { kind: 'text', text: 'IBKR, read-only, through the data plane\'s retained Gateway session. Real-time quotes only.' } },
    line('Connection', inputs.connection),
    line('Entitlement', inputs.entitlement),
    line('Subscription', inputs.subscription),
    ...(snapshot === null ? [] : [{ label: 'Snapshot', value: { kind: 'instant', ms: snapshot.as_of_ms, mode: 'local', granularity: 'time' } } satisfies ChainFact]),
    ...(snapshot === null ? [] : [{ label: 'Underlying', value: { kind: 'text', text: underlyingText(inputs.symbol, snapshot.underlying_price) } } satisfies ChainFact]),
    ...(inputs.expiryMs === null ? [] : [{ label: 'Expiry', value: { kind: 'expiry', ms: inputs.expiryMs, tag: null } } satisfies ChainFact]),
  ];
}

/** The live chain as the one chain presentation. */
export function livePresentation(inputs: LiveChainInputs): ChainPresentation {
  const { snapshot } = inputs;
  const context: CrossCheckContext | null = snapshot === null ? null : { live: inputs.live, snapshot, engine: inputs.engine };
  const { rows, cells } = snapshot === null || context === null ? { rows: [], cells: [] } : rowsOf(snapshot, context, inputs.answers);
  return {
    mode: 'now-live',
    underlying: inputs.symbol,
    facts: facts(inputs),
    notes: [],
    ...(inputs.serving ? { crossCheck: crossCheckStatement(inputs.engine, crossCheckOutcome(cells)) } : {}),
    table: {
      columns: COLUMNS,
      centre: false,
      rows,
      caption: inputs.expiryMs === null
        ? 'Calls on the left, puts on the right, strikes down the middle.'
        : `Calls on the left, puts on the right, strikes down the middle, for the ${optionExpiryDate(inputs.expiryMs)} expiry.`,
    },
  };
}
