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
 * The snapshot carries a quote, a day's bar, a last trade, Greeks and open interest per contract.
 * A figure the provider sent as nothing, or as 0 where 0 is no figure, is an absent field with the
 * reason that is true of it, and draws as a dash. The last price is a trade (the day's close or the
 * last trade) and never a quote's midpoint. A contract is a row of its own, so a standard and an
 * adjusted contract at one strike stay two rows.
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

/** What a figure the provider sent as a number is drawn as: its text when it is more than 0, 0 as "sent 0", and nothing sent as such. */
function figure(value: number | null | undefined, text: (value: number) => string, bar?: number): ChainField {
  if (value == null) return absent('not_in_snapshot');
  if (value === 0) return absent('sent_zero');
  return { kind: 'value', text: text(value), atMs: null, ...(bar === undefined ? {} : { bar }) };
}

const price = (value: number | null | undefined): ChainField => figure(value, (each) => each.toFixed(2));

/**
 * The last price the snapshot gives: the day's close, else its last trade. A quote is never a last
 * price, so with no trade the field is absent and says so: bid and ask are their own fields, and a
 * midpoint drawn as a price that traded would be a quote read as a print.
 */
function lastPrice(contract: SnapshotContractResult): ChainField {
  const close = contract.day?.close;
  if (close != null && close > 0) return { kind: 'value', text: close.toFixed(2), atMs: null };
  const trade = contract.lastTrade;
  if (trade == null) return absent('no_trade_in_snapshot');
  return trade.price != null && trade.price > 0 ? { kind: 'value', text: trade.price.toFixed(2), atMs: null } : absent('no_price_sent');
}

/** The ask less the bid, from a two-sided quote that is not crossed. */
function spread(bid: number | null | undefined, ask: number | null | undefined): ChainField {
  if (bid == null || ask == null || bid <= 0 || ask <= 0 || ask < bid) return absent('no_two_sided_quote');
  return { kind: 'value', text: (ask - bid).toFixed(2), atMs: null };
}

const greek = (value: number | null | undefined): ChainField =>
  value == null ? absent('not_in_snapshot') : { kind: 'value', text: value.toFixed(3), atMs: null };

const iv = (value: number | null | undefined): ChainField => figure(value, (each) => (each * 100).toFixed(2) + '%');

function count(value: number): string {
  if (value >= 1_000_000) return (value / 1_000_000).toFixed(1) + 'M';
  if (value >= 1_000) return (value / 1_000).toFixed(1) + 'K';
  return value.toLocaleString();
}

function barWidth(volume: number | null | undefined, max: number): number {
  if (!volume || !max) return 0;
  return Math.min(100, (volume / max) * 100);
}

/** The snapshot holds no engine delta: only Now · Live IBKR compares one against the feed's. */
const NO_COMPARISON = absent('not_compared', 'only Now · Live IBKR compares an engine delta');

function contractCell(contract: SnapshotContractResult | null, maxVolume: number, gap: string): ChainSideCell {
  if (contract === null) return { kind: 'gap', words: gap };
  const quote = contract.lastQuote;
  const cell: ChainContractCell = {
    kind: 'contract',
    symbol: contract.ticker ?? '',
    standard: contract.standard,
    fields: {
      last: lastPrice(contract),
      bid: price(quote?.bid),
      ask: price(quote?.ask),
      spread: spread(quote?.bid, quote?.ask),
      delta: greek(contract.greeks?.delta),
      gamma: greek(contract.greeks?.gamma),
      theta: greek(contract.greeks?.theta),
      vega: greek(contract.greeks?.vega),
      iv: iv(contract.impliedVolatility),
      volume: figure(contract.day?.volume, count, barWidth(contract.day?.volume, maxVolume)),
      openInterest: figure(contract.openInterest, count),
      engineDelta: NO_COMPARISON,
      deltaGap: NO_COMPARISON,
    },
  };
  return cell;
}

/** The figure between the sides: the two implied volatilities' mean where both are sent, else the one that is. */
function centreIv(call: SnapshotContractResult | null, put: SnapshotContractResult | null): ChainField {
  const sent = [call?.impliedVolatility, put?.impliedVolatility].filter((each): each is number => each != null);
  if (sent.length === 0) return absent('not_in_snapshot');
  const usable = sent.filter((each) => each > 0);
  if (usable.length === 0) return absent('sent_zero');
  return iv(usable.reduce((total, each) => total + each, 0) / usable.length);
}

/** The contracts of one right at one strike, a standard one before an adjusted one so the rows keep a stable order. */
function atStrike(contracts: readonly SnapshotContractResult[]): readonly SnapshotContractResult[] {
  const rank = (contract: SnapshotContractResult): number => (contract.standard === true ? 0 : contract.standard === null ? 1 : 2);
  return [...contracts].sort((a, b) => rank(a) - rank(b) || (a.ticker ?? '').localeCompare(b.ticker ?? ''));
}

interface StrikeContracts {
  readonly calls: SnapshotContractResult[];
  readonly puts: SnapshotContractResult[];
}

/**
 * The rows the snapshot shows: every listed strike, or those within `range` strikes of the money.
 * A strike lists every contract the provider sent for it: a standard and an adjusted call at one
 * strike are two rows, never one hiding the other, as in the archive's chain.
 */
function snapshotRows(inputs: SnapshotChainInputs, spot: number): ChainRow[] {
  const { contracts, strikeRange, showAll } = inputs;
  if (contracts.length === 0) return [];

  const byStrike = new Map<number, StrikeContracts>();
  for (const contract of contracts) {
    if (contract.strikePrice == null) continue;
    const here = byStrike.get(contract.strikePrice) ?? { calls: [], puts: [] };
    if (contract.contractType === 'call') here.calls.push(contract);
    else if (contract.contractType === 'put') here.puts.push(contract);
    byStrike.set(contract.strikePrice, here);
  }
  const strikes = [...byStrike.keys()].sort((a, b) => a - b);

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
    const here = byStrike.get(strike);
    for (const call of here?.calls ?? []) maxCallVolume = Math.max(maxCallVolume, call.day?.volume ?? 0);
    for (const put of here?.puts ?? []) maxPutVolume = Math.max(maxPutVolume, put.day?.volume ?? 0);
  }

  return visible.flatMap<ChainRow>((strike) => {
    const here = byStrike.get(strike);
    const calls = atStrike(here?.calls ?? []);
    const puts = atStrike(here?.puts ?? []);
    const money = strike === atmStrike;
    const rows: ChainRow[] = [];
    for (let place = 0; place < Math.max(calls.length, puts.length); place += 1) {
      const call = calls.at(place) ?? null;
      const put = puts.at(place) ?? null;
      const gap = place === 0 ? 'No contract at this strike' : 'No other contract at this strike';
      const atm = money && place === 0;
      rows.push({
        key: `${strike}:${place}`,
        strike: strike.toFixed(2),
        atm,
        mark: atm && spot > 0 ? `ATM ${spot.toFixed(2)}` : null,
        itm: spot > 0 && !money ? (strike < spot ? 'call' : strike > spot ? 'put' : null) : null,
        centre: centreIv(call, put),
        call: contractCell(call, maxCallVolume, gap),
        put: contractCell(put, maxPutVolume, gap),
      });
    }
    return rows;
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
