import { formatAuthoredUsd } from '../../../../shared/pipes/authored-usd.pipe';
import { formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';
import { formatTimestampDisplay } from '../../../../shared/timestamp';
import type { WorkspaceStateContent } from '../../../../shared/workspace/workspace-state';
import {
  absent,
  chainAbsentWords,
  type ChainContractCell,
  type ChainFact,
  type ChainField,
  type ChainPresentation,
  type ChainRow,
  type ChainSideCell,
} from '../chain-presentation';
import {
  bodyOf,
  cellOf,
  underlyingViewOf,
  type ChainBody,
  type ChainView,
  type DayView,
  type Gap,
  type NoStrikesWhy,
  type StrikeRow,
} from './past-chain-view';
import type { ChainContract, ChainExpiry, ExpirySummary, FetchRefusal } from './past-chain.types';

/*
 * Past · Archive: what the data lake's archive held for one underlying, mapped into the one
 * chain presentation.
 *
 * Two rules are structural here. First, a value observed after the instant the chain is read
 * at is never drawn: the server reads no later bar, and a value whose time is later anyway is
 * an absent field with that reason. Second, the archive holds each contract's minute trade
 * bars, so the one price a contract has is a last trade, in the `last` field with the minute
 * it is the close of; there is no `bid`, `ask`, Greek or open interest to fill, and the table
 * says so rather than drawing a column of dashes.
 *
 * A price is the server's authored dollar string, drawn textually and never turned into a
 * number (PRD #2560 D12).
 */

/** What an empty cell says, by why it is empty. */
const GAP_WORDS: Readonly<Record<Gap, string>> = {
  not_listed: 'Not listed at this strike',
  no_other: 'No other contract at this strike',
  outside: 'Nothing in this window',
};

const dateOf = (ms: number): string => formatTimestampDisplay(ms, { mode: 'date-et', granularity: 'date' });

/** What the listing says of one expiry, for its hover note in the ribbon: its series, its distance and its counts as served. */
export function expiryNote(expiry: ExpirySummary): string {
  const today = expiry.tag.expires_today ? ', expires today' : '';
  return `${formatReceiptLabel(expiry.tag.series)}${today}. ${expiry.tag.days_to_expiry} days to expiry. Calls: ${expiry.calls} listed, ${expiry.standard_calls} standard. Puts: ${expiry.puts} listed, ${expiry.standard_puts} standard.`;
}

/** A contract's last trade by the instant, or the plain reason it has none. */
function priceField(contract: ChainContract, atMs: number): ChainField {
  const cell = cellOf(contract);
  switch (cell.kind) {
    case 'traded':
      return cell.timeMs > atMs
        ? absent('after_instant')
        : { kind: 'value', text: formatAuthoredUsd(cell.price) ?? cell.price, atMs: cell.timeMs };
    case 'incomplete':
      return absent(cell.missing === 'price' ? 'no_price_sent' : 'no_time_sent');
    case 'did_not_trade':
      return absent('did_not_trade');
    case 'not_stored':
      return absent('not_stored');
    case 'unreadable':
      return absent('unreadable', cell.problem);
    case 'unknown':
      return absent('unknown_state', formatReceiptLabel(cell.code));
  }
}

/** The underlying's own price at the instant, by the same rule. */
function underlyingField(chain: ChainExpiry): ChainField {
  const view = underlyingViewOf(chain.underlying_price);
  switch (view.kind) {
    case 'priced':
      return view.timeMs > chain.at_ms
        ? absent('after_instant')
        : { kind: 'value', text: formatAuthoredUsd(view.price) ?? view.price, atMs: view.timeMs };
    case 'incomplete':
      return absent(view.missing === 'price' ? 'no_price_sent' : 'no_time_sent');
    case 'no_bar_yet':
      return absent('no_bar_yet');
    case 'not_stored':
      return absent('bars_not_stored');
    case 'unreadable':
      return absent('bars_unreadable', view.problem);
    case 'unknown':
      return absent('unknown_state', formatReceiptLabel(view.code));
  }
}

/** The archive holds trade bars and no recorded snapshot: every field but the last trade is absent, each for its own reason. */
const NO_QUOTE = absent('no_quote_archived');
const NO_SNAPSHOT = absent('no_recorded_snapshot');

function sideCell(contract: ChainContract | null, gap: Gap | null, atMs: number): ChainSideCell {
  if (contract === null) return { kind: 'gap', words: GAP_WORDS[gap ?? 'outside'] };
  const cell: ChainContractCell = {
    kind: 'contract',
    symbol: contract.symbol,
    standard: contract.standard,
    fields: {
      last: priceField(contract, atMs),
      bid: NO_QUOTE,
      ask: NO_QUOTE,
      spread: NO_QUOTE,
      delta: NO_SNAPSHOT,
      gamma: NO_SNAPSHOT,
      theta: NO_SNAPSHOT,
      vega: NO_SNAPSHOT,
      volume: NO_SNAPSHOT,
      openInterest: NO_SNAPSHOT,
    },
  };
  return cell;
}

function moneyMark(row: StrikeRow): string | null {
  if (row.callMoney && row.putMoney) return 'At the money';
  if (row.callMoney) return 'At the money for calls';
  return row.putMoney ? 'At the money for puts' : null;
}

function chainRow(row: StrikeRow, atMs: number): ChainRow {
  return {
    key: row.key,
    strike: formatAuthoredUsd(row.strike) ?? row.strike,
    atm: row.callMoney || row.putMoney,
    mark: moneyMark(row),
    itm: null,
    centre: null,
    call: sideCell(row.call, row.callGap, atMs),
    put: sideCell(row.put, row.putGap, atMs),
  };
}

const SOURCE_WORDS =
  "The data lake's archive: the listing stored for the session, and each contract's minute trade bars. Nothing observed after the instant is used.";

function facts(chain: ChainExpiry): readonly ChainFact[] {
  const { calls, puts } = chain;
  return [
    { label: 'Mode', value: { kind: 'text', text: 'Past · Archive' } },
    { label: 'Source', value: { kind: 'text', text: SOURCE_WORDS } },
    { label: 'Session', value: { kind: 'date', ms: chain.session_open_ms } },
    { label: 'Priced as of', value: { kind: 'instant', ms: chain.at_ms, mode: 'et', granularity: 'minute' } },
    { label: `${chain.underlying} then`, value: { kind: 'field', field: underlyingField(chain) } },
    { label: 'Expiry', value: { kind: 'expiry', ms: chain.expiry_ms, tag: chain.tag } },
    {
      label: 'Contracts',
      value: { kind: 'text', text: `Calls: ${calls.listed} listed, ${calls.standard_listed} standard. Puts: ${puts.listed} listed, ${puts.standard_listed} standard.` },
    },
    { label: 'Strikes each side of the money', value: { kind: 'text', text: String(chain.strikes_each_side) } },
  ];
}

/** What the archive cannot say, in the words of the reasons a field is absent: a last trade is never a quote. */
const NOT_ARCHIVED = [
  `${chainAbsentWords(NO_QUOTE)}: a price here is a last trade, never a bid or an ask.`,
  `${chainAbsentWords(NO_SNAPSHOT)}, so Greeks, open interest and volume are not shown for it. No current value stands in for a past one.`,
];

function undrawnNote(side: 'calls' | 'puts'): string {
  return `No strikes are drawn for ${side}: the underlying has a price, but the server found no standard contract on that side to centre them on.`;
}

/**
 * The archive chain of one expiry as the one chain presentation. A body that draws no
 * strikes (nothing listed, or no price to centre them on) has no rows; `archiveBodyState`
 * says why.
 */
export function archivePresentation(chain: ChainExpiry, body: ChainBody = bodyOf(chain)): ChainPresentation {
  const rows = body.kind === 'rows' ? body.rows.map((row) => chainRow(row, chain.at_ms)) : [];
  return {
    mode: 'past-archive',
    underlying: chain.underlying,
    expiryMs: chain.expiry_ms,
    facts: facts(chain),
    notes: [...NOT_ARCHIVED, ...(body.kind === 'rows' ? body.undrawn.map(undrawnNote) : [])],
    table: {
      columns: { calls: ['last'], puts: ['last'], detail: 'full' },
      centre: false,
      rows,
      caption: `Calls on the left, puts on the right, strikes down the middle, for the ${dateOf(chain.expiry_ms)} expiry.`,
    },
  };
}

// ---- States --------------------------------------------------------------------------------

/** Why no strikes are drawn although contracts are listed, and what helps. */
function noStrikesWords(why: NoStrikesWhy, downloadOffered: boolean): { readonly why: string; readonly next: string | null } {
  if (why.kind === 'no_ladder') return { why: 'The underlying has a price, but the server found no standard contract on a side to centre them on.', next: null };
  if (why.kind === 'empty_window') return { why: 'The server sent no contract in this window.', next: null };
  const under = why.underlying;
  switch (under.kind) {
    case 'no_bar_yet':
      return {
        why: 'No bar of the underlying had closed by this time, so there is no price to centre them on.',
        next: why.laterTime ? 'Pick a later time to see them.' : null,
      };
    case 'not_stored':
      return {
        why: "The underlying's bars for this date are not stored, so there is no price to centre them on.",
        next: downloadOffered ? 'Download missing data above fetches them.' : null,
      };
    case 'unreadable':
      return { why: "The underlying's bars for this date cannot be read, so there is no price to centre them on.", next: null };
    case 'unknown':
      return {
        why: `This page does not know this state of the underlying's price, ${formatReceiptLabel(under.code)}, so it cannot say why there is no price to centre them on.`,
        next: null,
      };
    case 'priced':
    case 'incomplete':
      return { why: 'The server sent an incomplete price for the underlying, so there is no price to centre them on.', next: null };
  }
}

/** The state a chain with no table to draw is in, or `null` when it draws one. */
export function archiveBodyState(chain: ChainExpiry, body: ChainBody, downloadOffered: boolean): WorkspaceStateContent | null {
  const counts = `Calls listed: ${chain.calls.listed}. Puts listed: ${chain.puts.listed}.`;
  if (body.kind === 'nothing_listed') {
    return { kind: 'idle', happened: 'No contract is listed at this expiry.', why: counts };
  }
  if (body.kind === 'no_strikes') {
    const words = noStrikesWords(body.why, downloadOffered);
    return { kind: 'unavailable', happened: 'No strikes are drawn.', why: `${words.why} ${counts}`, next: words.next };
  }
  return null;
}

/** The words for the control a chain waits on. */
const CONTROL_NAMES = { underlying: 'underlying', date: 'trading date', time: 'time of day', strikes: 'strikes box' } as const;

/** The state the chain card is in when it has no chain to draw, or `null` when it has one. */
export function archiveChainState(view: ChainView, expiryMs: number | null): WorkspaceStateContent | null {
  const expiry = expiryMs === null ? 'picked' : dateOf(expiryMs);
  switch (view.kind) {
    case 'no_day':
    case 'shown':
      return null;
    case 'no_expiry':
      return { kind: 'idle', happened: 'No expiry is open.', next: 'Pick one in the expiry ribbon to read its chain. None is opened for you.' };
    case 'blocked':
      return {
        kind: 'invalid',
        happened: `The chain is not read while the ${CONTROL_NAMES[view.control]} holds something it cannot be asked with.`,
        next: 'Its reason is at that control.',
      };
    case 'loading':
      return { kind: 'checking', happened: 'Reading the chain.' };
    case 'failed':
      return {
        kind: 'failed',
        happened: view.message,
        why: view.code === null ? null : formatReceiptLabel(view.code),
        actions: view.canRetry ? [{ id: 'retry-chain', label: 'Try again' }] : [],
      };
    case 'not_listed':
      return {
        kind: 'unavailable',
        happened: `The ${expiry} expiry is not listed on this date.`,
        next: 'The expiries that are listed are in the ribbon above; none has been opened for you.',
      };
    case 'listing_unknown':
      return {
        kind: 'unavailable',
        happened: `Whether the ${expiry} expiry is listed cannot be said: the listing for this date is ${view.state === 'unreadable' ? 'stored but cannot be read' : 'not stored'}.`,
        why: view.problem,
      };
    case 'unknown_listing':
      return {
        kind: 'unavailable',
        happened: `This page does not know this state of the listing, ${formatReceiptLabel(view.code)}, so it does not say whether the ${expiry} expiry is listed.`,
      };
  }
}

/** The state the session's listing is in when it lists no expiry to pick from, or `null` when it lists some. */
export function archiveDayState(view: DayView): WorkspaceStateContent | null {
  switch (view.kind) {
    case 'pick':
      return { kind: 'idle', happened: 'No session date is picked.', next: 'Pick a past session date to read what the archive listed that day. Nothing is read until you do.' };
    case 'loading':
      return { kind: 'checking', happened: 'Reading the listing.' };
    case 'not_a_session':
      return { kind: 'invalid', happened: 'Not a trading day.', why: view.message };
    case 'at_control':
      return {
        kind: 'invalid',
        happened: `The server did not take the ${view.control === 'underlying' ? 'instrument' : 'trading date'}; its reason is at that control.`,
      };
    case 'failed':
      return {
        kind: 'failed',
        happened: view.message,
        why: view.code === null ? null : formatReceiptLabel(view.code),
        actions: view.canRetry ? [{ id: 'retry-day', label: 'Try again' }] : [],
      };
    case 'not_stored':
      return { kind: 'unavailable', happened: 'The listing for this date is not stored.' };
    case 'unreadable':
      return { kind: 'failed', happened: 'The stored listing for this date cannot be read.', why: view.problem };
    case 'unknown_listing':
      return { kind: 'unavailable', happened: `This page does not know this state of the listing for this date: ${formatReceiptLabel(view.code)}.` };
    case 'empty':
      return { kind: 'idle', happened: 'The listing for this date is stored, but it holds no contract a backtest can use.' };
    case 'listed':
      return null;
  }
}

/** The lake's own words for a session it will fetch nothing for now, with its code in words under them. */
export function refusalWords(refusal: FetchRefusal): { readonly words: string; readonly code: string } {
  return { words: refusal.words, code: formatReceiptLabel(refusal.reason) };
}
