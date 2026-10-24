import type {
  SnapshotContractResult,
  SnapshotUnderlyingResult,
  StockTickerSnapshot,
} from '../../../../services/market-data.types';
import { optionExpiryDate } from '../../../../shared/option-contract/option-expiry-date';
import { daysBetween } from '../../../../shared/ticker-range-picker/ticker-range-picker.types';
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

/*
 * Now · Snapshot: the provider's current option-chain snapshot, mapped into the one chain
 * presentation. The strike window, the money strike, the shading and every figure's format
 * are the Options Lab chain's, moved here unchanged.
 *
 * The snapshot carries a quote, a day's bar, Greeks and open interest per contract. A figure
 * the provider sent as nothing, or as zero where zero is no figure, is an absent field and
 * draws as a dash.
 */

export type ChainDensity = 'quick' | 'greeks' | 'flow';

const NO_FIGURE = '—';

/** The columns each density draws, calls outward to inward and puts inward to outward. */
const DENSITY_COLUMNS: Readonly<Record<ChainDensity, ChainColumns>> = {
  quick: { calls: ['delta', 'last', 'volume'], puts: ['volume', 'last', 'delta'], detail: 'compact' },
  greeks: {
    calls: ['vega', 'theta', 'gamma', 'delta', 'last', 'bid', 'ask', 'spread', 'volume'],
    puts: ['volume', 'spread', 'bid', 'ask', 'last', 'delta', 'gamma', 'theta', 'vega'],
    detail: 'compact',
  },
  flow: { calls: ['bid', 'ask', 'openInterest', 'volume'], puts: ['volume', 'openInterest', 'bid', 'ask'], detail: 'compact' },
};

export interface SnapshotChainInputs {
  readonly symbol: string;
  readonly expiryMs: number;
  readonly contracts: readonly SnapshotContractResult[];
  readonly underlying: SnapshotUnderlyingResult | null;
  readonly stock: StockTickerSnapshot | null;
  /** Strikes each side of the money to show, unless `showAll`. */
  readonly strikeRange: number;
  readonly showAll: boolean;
  readonly density: ChainDensity;
  /** When the chain on screen arrived (`int64 ms UTC`). */
  readonly receivedAtMs: number | null;
  /** Why the latest background refresh failed, so quotes older than it read as stale. */
  readonly refreshError: string | null;
  /** Today's New York date, which the days to expiry are counted from. */
  readonly todayEt: string;
}

/** The spot the chain is centred on: the stock's own day close, else the chain's underlying price (0 when neither is known). */
function snapshotSpot(stock: StockTickerSnapshot | null, underlying: SnapshotUnderlyingResult | null): number {
  if (stock?.day?.close != null && stock.day.close > 0) return stock.day.close;
  return underlying?.price ?? 0;
}

function fmtPrice(value: number | null | undefined): string | null {
  return value != null && value > 0 ? value.toFixed(2) : null;
}

/** The last price the snapshot gives: the day's close, else its last trade, else the quote's midpoint. */
function resolveLast(contract: SnapshotContractResult | null): string | null {
  if (!contract) return null;
  if (contract.day?.close != null && contract.day.close > 0) return contract.day.close.toFixed(2);
  if (contract.lastTrade?.price != null && contract.lastTrade.price > 0) return contract.lastTrade.price.toFixed(2);
  if (contract.lastQuote?.midpoint != null && contract.lastQuote.midpoint > 0) return contract.lastQuote.midpoint.toFixed(2);
  const bid = contract.lastQuote?.bid ?? 0;
  const ask = contract.lastQuote?.ask ?? 0;
  if (bid > 0 && ask > 0) return ((bid + ask) / 2).toFixed(2);
  return null;
}

function fmtSpread(bid: number | null | undefined, ask: number | null | undefined): string | null {
  if (bid == null || ask == null || bid <= 0 || ask <= 0 || ask < bid) return null;
  return (ask - bid).toFixed(2);
}

function fmtGreek(value: number | null | undefined): string | null {
  return value != null ? value.toFixed(3) : null;
}

function fmtIv(value: number | null | undefined): string | null {
  return value != null && value > 0 ? (value * 100).toFixed(2) + '%' : null;
}

function fmtVolume(value: number | null | undefined): string | null {
  if (value == null || value === 0) return null;
  if (value >= 1_000_000) return (value / 1_000_000).toFixed(1) + 'M';
  if (value >= 1_000) return (value / 1_000).toFixed(1) + 'K';
  return value.toLocaleString();
}

function barWidth(volume: number | null | undefined, max: number): number {
  if (!volume || !max) return 0;
  return Math.min(100, (volume / max) * 100);
}

function field(text: string | null, bar?: number): ChainField {
  return text === null ? absent('not_in_snapshot') : { kind: 'value', text, atMs: null, ...(bar === undefined ? {} : { bar }) };
}

/** The snapshot holds no engine delta: only Now · Live IBKR compares one against the feed's. */
const NO_COMPARISON = absent('not_compared', 'only Now · Live IBKR compares an engine delta');

function contractCell(contract: SnapshotContractResult | null, maxVolume: number): ChainSideCell {
  if (contract === null) return { kind: 'gap', words: 'No contract at this strike' };
  const cell: ChainContractCell = {
    kind: 'contract',
    symbol: contract.ticker ?? '',
    standard: null,
    fields: {
      last: field(resolveLast(contract)),
      bid: field(fmtPrice(contract.lastQuote?.bid)),
      ask: field(fmtPrice(contract.lastQuote?.ask)),
      spread: field(fmtSpread(contract.lastQuote?.bid, contract.lastQuote?.ask)),
      delta: field(fmtGreek(contract.greeks?.delta)),
      gamma: field(fmtGreek(contract.greeks?.gamma)),
      theta: field(fmtGreek(contract.greeks?.theta)),
      vega: field(fmtGreek(contract.greeks?.vega)),
      iv: field(fmtIv(contract.impliedVolatility)),
      volume: field(fmtVolume(contract.day?.volume), barWidth(contract.day?.volume, maxVolume)),
      openInterest: field(fmtVolume(contract.openInterest)),
      engineDelta: NO_COMPARISON,
      deltaGap: NO_COMPARISON,
    },
  };
  return cell;
}

function centreIv(call: SnapshotContractResult | null, put: SnapshotContractResult | null): ChainField {
  const callIv = call?.impliedVolatility ?? 0;
  const putIv = put?.impliedVolatility ?? 0;
  const centre = callIv > 0 && putIv > 0 ? (callIv + putIv) / 2 : callIv || putIv;
  return field(fmtIv(centre));
}

/** The rows the snapshot shows: every listed strike, or those within `range` strikes of the money. */
function snapshotRows(inputs: SnapshotChainInputs, spot: number): ChainRow[] {
  const { contracts, strikeRange, showAll } = inputs;
  if (contracts.length === 0) return [];

  const callMap = new Map<number, SnapshotContractResult>();
  const putMap = new Map<number, SnapshotContractResult>();
  const strikeSet = new Set<number>();
  for (const contract of contracts) {
    if (contract.strikePrice == null) continue;
    strikeSet.add(contract.strikePrice);
    if (contract.contractType === 'call') callMap.set(contract.strikePrice, contract);
    else if (contract.contractType === 'put') putMap.set(contract.strikePrice, contract);
  }
  const strikes = [...strikeSet].sort((a, b) => a - b);

  let atmStrike: number | null = null;
  if (spot > 0 && strikes.length > 0) {
    let nearest = Infinity;
    for (const strike of strikes) {
      const distance = Math.abs(strike - spot);
      if (distance < nearest) {
        atmStrike = strike;
        nearest = distance;
      }
    }
  }

  let visible = strikes;
  if (!showAll && atmStrike != null) {
    const at = strikes.indexOf(atmStrike);
    if (at !== -1) visible = strikes.slice(Math.max(0, at - strikeRange), Math.min(strikes.length, at + strikeRange + 1));
  }

  let maxCallVolume = 0;
  let maxPutVolume = 0;
  for (const strike of visible) {
    maxCallVolume = Math.max(maxCallVolume, callMap.get(strike)?.day?.volume ?? 0);
    maxPutVolume = Math.max(maxPutVolume, putMap.get(strike)?.day?.volume ?? 0);
  }

  return visible.map<ChainRow>((strike) => {
    const call = callMap.get(strike) ?? null;
    const put = putMap.get(strike) ?? null;
    const atm = strike === atmStrike;
    return {
      key: String(strike),
      strike: strike.toFixed(2),
      atm,
      mark: atm && spot > 0 ? `ATM ${spot.toFixed(2)}` : null,
      itm: spot > 0 && !atm ? (strike < spot ? 'call' : strike > spot ? 'put' : null) : null,
      centre: centreIv(call, put),
      call: contractCell(call, maxCallVolume),
      put: contractCell(put, maxPutVolume),
    };
  });
}

type ProviderLabel = 'delayed' | 'real-time' | 'unstated';

/** What the provider's own `timeframe` labels on the chain's quotes and trades say about their age. */
function providerLabel(contracts: readonly SnapshotContractResult[]): ProviderLabel {
  const labels = contracts
    .flatMap((contract) => [contract.lastQuote?.timeframe, contract.lastTrade?.timeframe])
    .filter((label): label is string => typeof label === 'string' && label !== '')
    .map((label) => label.toLowerCase());
  if (labels.some((label) => label.includes('delay'))) return 'delayed';
  return labels.length > 0 && labels.every((label) => label.includes('real')) ? 'real-time' : 'unstated';
}

const PROVIDER_WORDS: Readonly<Record<ProviderLabel, { text: string; tone?: 'warn' }>> = {
  delayed: { text: 'Delayed: the provider labels these quotes delayed.', tone: 'warn' },
  'real-time': { text: 'Real-time: the provider labels these quotes real-time.' },
  unstated: { text: 'Not stated: the provider does not say whether these quotes are delayed.' },
};

function underlyingText(symbol: string, spot: number, stock: StockTickerSnapshot | null): string {
  if (spot <= 0) return `${symbol} ${NO_FIGURE}`;
  const change = stock?.todaysChange ?? null;
  const percent = stock?.todaysChangePercent ?? null;
  if (change === null || percent === null) return `${symbol} ${spot.toFixed(2)}, move ${NO_FIGURE}`;
  const sign = (value: number) => (value > 0 ? '+' : '');
  return `${symbol} ${spot.toFixed(2)}, ${sign(change)}${change.toFixed(2)} (${sign(percent)}${percent.toFixed(2)}%)`;
}

/** Calendar days between today's New York date and the date the anchor names, so the whole of expiry day reads 0 DTE (#3221). */
function daysToExpiry(expiryMs: number, todayEt: string): number {
  return Math.max(daysBetween(todayEt, optionExpiryDate(expiryMs)), 0);
}

function facts(inputs: SnapshotChainInputs, spot: number): readonly ChainFact[] {
  const provider = PROVIDER_WORDS[providerLabel(inputs.contracts)];
  const stale = inputs.refreshError;
  return [
    { label: 'Mode', value: { kind: 'text', text: 'Now · Snapshot' } },
    { label: 'Source', value: { kind: 'text', text: 'Polygon option-chain snapshot, read through the data service.' } },
    ...(inputs.receivedAtMs === null
      ? []
      : [{ label: 'Quotes received', value: { kind: 'instant', ms: inputs.receivedAtMs, mode: 'local', granularity: 'time' } } satisfies ChainFact]),
    stale === null
      ? { label: 'Quote state', value: { kind: 'text', text: provider.text }, ...(provider.tone === undefined ? {} : { tone: provider.tone }) }
      : { label: 'Quote state', value: { kind: 'text', text: `Stale: the last refresh failed (${stale}). These quotes are from the last refresh that worked.` }, tone: 'bad' },
    { label: 'Underlying', value: { kind: 'text', text: underlyingText(inputs.symbol, spot, inputs.stock) } },
    { label: 'Expiry', value: { kind: 'expiry', ms: inputs.expiryMs, tag: null } },
    { label: 'Days to expiry', value: { kind: 'text', text: `${daysToExpiry(inputs.expiryMs, inputs.todayEt)} DTE` } },
  ];
}

/** The snapshot as the one chain presentation. */
export function snapshotPresentation(inputs: SnapshotChainInputs): ChainPresentation {
  const spot = snapshotSpot(inputs.stock, inputs.underlying);
  return {
    mode: 'now-snapshot',
    underlying: inputs.symbol,
    expiryMs: inputs.expiryMs,
    facts: facts(inputs, spot),
    notes: [],
    table: {
      columns: DENSITY_COLUMNS[inputs.density],
      centre: true,
      rows: snapshotRows(inputs, spot),
      caption: `Calls on the left, puts on the right, strikes down the middle, for the ${optionExpiryDate(inputs.expiryMs)} expiry.`,
    },
  };
}
