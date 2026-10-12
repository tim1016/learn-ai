import type { ResourceStatus } from '@angular/core';

import { NOT_A_SESSION, type ControlName, type ReadProblem } from './past-chain-problems';
import type { ChainContract, ChainDay, ChainExpiry, ChainSide, DownloadAnswer, DownloadRequest, SessionBounds, UnderlyingPrice } from './past-chain.types';

/**
 * What the tab shows, decided from the server's state codes and from nothing else. Each state is a kind here, so that a
 * template cannot forget one and a spec can name it, and a code this page does not know is a kind of its own, said in
 * words. Something is said to be absent only where a field of the answer proves it: missing data is never a missing
 * contract. Nothing in this file prices a contract, builds a symbol, or does arithmetic on a served figure: strikes are
 * compared, counts are counted.
 */

/** The session of a trading date as one of the three answers states it. */
export function sessionOf(answer: Pick<ChainDay, 'session_open_ms' | 'session_close_ms'>): SessionBounds {
  return { openMs: answer.session_open_ms, closeMs: answer.session_close_ms };
}

// ---- The day ------------------------------------------------------------------------------------

export type DayView =
  /** No underlying or no usable date yet: there is nothing to read. */
  | { readonly kind: 'pick' }
  | { readonly kind: 'loading' }
  /** The date is not a trading day (the server's sentence says which kind of day it is). */
  | { readonly kind: 'not_a_session'; readonly message: string }
  /** The server would not take the underlying or the date; its reason is at that control. */
  | { readonly kind: 'at_control'; readonly control: 'underlying' | 'date' }
  /** The read failed: the catalog is out of reach, the call never arrived, or the server refused for a reason of its own. */
  | { readonly kind: 'failed'; readonly message: string; readonly code: string | null; readonly canRetry: boolean }
  | { readonly kind: 'not_stored' }
  | { readonly kind: 'unreadable'; readonly problem: string | null }
  /** The server named a listing state this page does not know. */
  | { readonly kind: 'unknown_listing'; readonly code: string }
  /** The listing is stored and holds no contract a backtest can use. */
  | { readonly kind: 'empty'; readonly day: ChainDay }
  | { readonly kind: 'listed'; readonly day: ChainDay };

/** What a resource's status comes to for the tab: nothing asked, asking, failed, or answered. */
export function readStatusOf(status: ResourceStatus): 'idle' | 'loading' | 'error' | 'ready' {
  if (status === 'idle') return 'idle';
  if (status === 'loading' || status === 'reloading') return 'loading';
  return status === 'error' ? 'error' : 'ready';
}

export function dayViewOf(status: ResourceStatus, day: ChainDay | null, problem: ReadProblem | null): DayView {
  const read = readStatusOf(status);
  if (read === 'idle') return { kind: 'pick' };
  if (read === 'loading') return { kind: 'loading' };
  if (read === 'error' || day === null) return problem === null ? { kind: 'failed', message: 'The listing could not be read.', code: null, canRetry: true } : failedDay(problem);
  return listingOf(day);
}

function listingOf(day: ChainDay): DayView {
  const state: string = day.listing_state;
  if (state === 'not_stored') return { kind: 'not_stored' };
  if (state === 'unreadable') return { kind: 'unreadable', problem: day.listing_problem };
  if (state !== 'stored') return { kind: 'unknown_listing', code: state };
  return day.expiries.length === 0 ? { kind: 'empty', day } : { kind: 'listed', day };
}

function failedDay(problem: ReadProblem): DayView {
  if (problem.code === NOT_A_SESSION) return { kind: 'not_a_session', message: problem.message };
  if (problem.control === 'underlying' || problem.control === 'date') return { kind: 'at_control', control: problem.control };
  return { kind: 'failed', message: problem.message, code: problem.code, canRetry: problem.canRetry };
}

// ---- One expiry ---------------------------------------------------------------------------------

export type ChainView =
  /** The day is not readable, so there is nothing to put a chain on: the day's card says why. */
  | { readonly kind: 'no_day' }
  | { readonly kind: 'no_expiry' }
  /** A control holds something the chain cannot be asked with, or something the server refused: the reason is at that control. */
  | { readonly kind: 'blocked'; readonly control: ControlName }
  | { readonly kind: 'loading' }
  | { readonly kind: 'failed'; readonly message: string; readonly code: string | null; readonly canRetry: boolean }
  /** The picked expiry is not in the listing of this date. The day's expiries stay on screen; none is picked for the owner. */
  | { readonly kind: 'not_listed' }
  /** Whether the expiry is listed cannot be said: the listing is not stored, or cannot be read. */
  | { readonly kind: 'listing_unknown'; readonly state: 'not_stored' | 'unreadable'; readonly problem: string | null }
  /** The server named a listing state this page does not know, so what it says of the expiry is not taken. */
  | { readonly kind: 'unknown_listing'; readonly code: string }
  | { readonly kind: 'shown'; readonly chain: ChainExpiry };

export interface ChainInputs {
  readonly dayReady: boolean;
  readonly expiryPicked: boolean;
  readonly blockedBy: ControlName | null;
  readonly status: ResourceStatus;
  readonly chain: ChainExpiry | null;
  readonly problem: ReadProblem | null;
}

export function chainViewOf(inputs: ChainInputs): ChainView {
  if (!inputs.dayReady) return { kind: 'no_day' };
  if (!inputs.expiryPicked) return { kind: 'no_expiry' };
  if (inputs.blockedBy !== null) return { kind: 'blocked', control: inputs.blockedBy };
  const read = readStatusOf(inputs.status);
  const chain = inputs.chain;
  if (read === 'idle' || read === 'loading') return { kind: 'loading' };
  if (chain !== null && read !== 'error') return chainOf(chain);
  const problem = inputs.problem;
  if (problem === null) return { kind: 'failed', message: 'The chain could not be read.', code: null, canRetry: true };
  // A refusal about a control is said at that control; the table has nothing to show meanwhile.
  return problem.control === 'area' ? { kind: 'failed', message: problem.message, code: problem.code, canRetry: problem.canRetry } : { kind: 'blocked', control: problem.control };
}

function chainOf(chain: ChainExpiry): ChainView {
  const listing: string = chain.listing_state;
  if (listing !== 'stored' && listing !== 'not_stored' && listing !== 'unreadable') return { kind: 'unknown_listing', code: listing };
  if (chain.expiry_listed === false) return { kind: 'not_listed' };
  if (chain.expiry_listed === null) {
    // The server says null only for a listing it could not read; a stored listing with no answer is a fault in the answer.
    if (listing === 'stored') return { kind: 'failed', message: 'The server did not say whether the expiry is listed.', code: null, canRetry: false };
    return { kind: 'listing_unknown', state: listing === 'unreadable' ? 'unreadable' : 'not_stored', problem: chain.listing_problem };
  }
  return { kind: 'shown', chain };
}

// ---- The table ----------------------------------------------------------------------------------

/**
 * Why a side shows no contract in a row. `not_listed` is said only when the answer proves it: the side lists nothing,
 * or shows every contract it lists, or the strike lies inside the strikes it shows (the server shows every contract a
 * side lists between its lowest and highest strike shown). `no_other` is the same proof for a strike where the side
 * has a contract in the row above. Anything else is only that the answer holds nothing there: `outside`.
 */
export type Gap = 'not_listed' | 'no_other' | 'outside';

/**
 * One row of the table: the call and the put listed at a strike (either may be absent), and where each side's money is.
 * Two contracts of one right at one strike are two rows, so no contract is hidden behind another.
 */
export interface StrikeRow {
  /** The strike and the place of this row among the rows of that strike: unique within the table. */
  readonly key: string;
  readonly thousandths: number;
  /** The strike in dollars, as the server wrote it. */
  readonly strike: string;
  readonly call: ChainContract | null;
  readonly put: ChainContract | null;
  /** Why the call (or put) cell is empty; null when it holds a contract. */
  readonly callGap: Gap | null;
  readonly putGap: Gap | null;
  /** This is the strike the calls' money was found at; likewise the puts'. The two sides' ladders can differ. It is marked once, on the first row of the strike. */
  readonly callMoney: boolean;
  readonly putMoney: boolean;
}

/**
 * The rows of both sides, ascending by strike. A strike is compared, never subtracted; a strike listed on one side only
 * is a row with the other side empty, and the reason it is empty is the answer's, not a guess.
 */
export function strikeRows(calls: ChainSide, puts: ChainSide): StrikeRow[] {
  const callsCover = coverOf(calls);
  const putsCover = coverOf(puts);
  const rows: StrikeRow[] = [];
  for (const thousandths of strikesOf(calls, puts)) {
    const callsHere = calls.contracts.filter((each) => each.strike_thousandths === thousandths);
    const putsHere = puts.contracts.filter((each) => each.strike_thousandths === thousandths);
    const height = callsHere.length > putsHere.length ? callsHere.length : putsHere.length;
    const strike = (callsHere.at(0) ?? putsHere.at(0))?.strike ?? '';
    for (let place = 0; place < height; place += 1) {
      const call = callsHere.at(place) ?? null;
      const put = putsHere.at(place) ?? null;
      rows.push({
        key: `${thousandths}:${place}`,
        thousandths,
        strike,
        call,
        put,
        callGap: call === null ? gapOf(callsHere.length > 0, callsCover(thousandths)) : null,
        putGap: put === null ? gapOf(putsHere.length > 0, putsCover(thousandths)) : null,
        callMoney: place === 0 && call !== null && calls.at_the_money_strike_thousandths === thousandths,
        putMoney: place === 0 && put !== null && puts.at_the_money_strike_thousandths === thousandths,
      });
    }
  }
  return rows;
}

function gapOf(hasOneHere: boolean, covered: boolean): Gap {
  if (hasOneHere) return 'no_other';
  return covered ? 'not_listed' : 'outside';
}

/** Every strike either side shows, once each, ascending. */
function strikesOf(calls: ChainSide, puts: ChainSide): number[] {
  const strikes = new Set<number>();
  for (const each of [...calls.contracts, ...puts.contracts]) strikes.add(each.strike_thousandths);
  return [...strikes].sort((a, b) => (a < b ? -1 : a > b ? 1 : 0));
}

/** Whether the answer says what a side lists at a strike: it lists nothing, shows all it lists, or the strike is inside the strikes it shows. */
function coverOf(side: ChainSide): (thousandths: number) => boolean {
  if (side.contracts.length >= side.listed) return () => true;
  const first = side.contracts.at(0);
  if (first === undefined) return () => false;
  let low = first.strike_thousandths;
  let high = first.strike_thousandths;
  for (const each of side.contracts) {
    if (each.strike_thousandths < low) low = each.strike_thousandths;
    if (each.strike_thousandths > high) high = each.strike_thousandths;
  }
  return (thousandths) => low <= thousandths && thousandths <= high;
}

/**
 * Why no strikes are drawn at all. The server sends none when the underlying has no price at the instant (there is no
 * money to centre them on), so the underlying's own state is the reason; with a price, a side lists contracts and has
 * no standard one to centre on; and otherwise the window is empty although contracts are listed.
 */
export type NoStrikesWhy =
  | { readonly kind: 'underlying'; readonly underlying: UnderlyingView; readonly laterTime: boolean }
  | { readonly kind: 'no_ladder' }
  | { readonly kind: 'empty_window' };

/** A side of the chain, named as the table's words name it. */
export type SideName = 'calls' | 'puts';

export type ChainBody =
  /** Both sides count nothing listed: the one case where the answer says nothing is there. */
  | { readonly kind: 'nothing_listed' }
  /** Contracts are listed and none is drawn: the table is not drawn, and the reason is. */
  | { readonly kind: 'no_strikes'; readonly why: NoStrikesWhy }
  /** Some side shows contracts. `undrawn` names a side that lists contracts and shows none, said beside the table. */
  | { readonly kind: 'rows'; readonly rows: readonly StrikeRow[]; readonly undrawn: readonly SideName[] };

export function bodyOf(chain: ChainExpiry): ChainBody {
  const callsShown = chain.calls.contracts.length > 0;
  const putsShown = chain.puts.contracts.length > 0;
  if (callsShown || putsShown) {
    const undrawn: SideName[] = [];
    if (!callsShown && chain.calls.listed > 0) undrawn.push('calls');
    if (!putsShown && chain.puts.listed > 0) undrawn.push('puts');
    return { kind: 'rows', rows: strikeRows(chain.calls, chain.puts), undrawn };
  }
  if (chain.calls.listed === 0 && chain.puts.listed === 0) return { kind: 'nothing_listed' };
  return { kind: 'no_strikes', why: noStrikesWhy(chain) };
}

function noStrikesWhy(chain: ChainExpiry): NoStrikesWhy {
  const underlying = underlyingViewOf(chain.underlying_price);
  if (underlying.kind !== 'priced') return { kind: 'underlying', underlying, laterTime: chain.at_ms < chain.session_close_ms };
  const noMoney = [chain.calls, chain.puts].some((side) => side.listed > 0 && side.at_the_money_strike_thousandths === null);
  return noMoney ? { kind: 'no_ladder' } : { kind: 'empty_window' };
}

/** What one contract's cell says. A price is only ever a `traded` cell, and a `traded` cell always carries its time. */
export type CellView =
  | { readonly kind: 'traded'; readonly price: string; readonly timeMs: number }
  /** The server said it traded and left out the price or the time of the trade: neither is guessed, and no price is drawn. */
  | { readonly kind: 'incomplete'; readonly missing: 'price' | 'time' }
  | { readonly kind: 'did_not_trade' }
  | { readonly kind: 'not_stored' }
  | { readonly kind: 'unreadable'; readonly problem: string | null }
  /** A price state this page does not know: its code, said in words, and no price. */
  | { readonly kind: 'unknown'; readonly code: string };

export function cellOf(contract: ChainContract): CellView {
  switch (contract.price_state) {
    case 'traded':
      if (contract.price === null) return { kind: 'incomplete', missing: 'price' };
      if (contract.price_time_ms === null) return { kind: 'incomplete', missing: 'time' };
      return { kind: 'traded', price: contract.price, timeMs: contract.price_time_ms };
    case 'did_not_trade':
      return { kind: 'did_not_trade' };
    case 'not_stored':
      return { kind: 'not_stored' };
    case 'unreadable':
      return { kind: 'unreadable', problem: contract.problem };
    default:
      return { kind: 'unknown', code: String(contract.price_state) };
  }
}

/** The underlying's own price at the instant, by the same rule: a price only with its time. */
export type UnderlyingView =
  | { readonly kind: 'priced'; readonly price: string; readonly timeMs: number }
  | { readonly kind: 'incomplete'; readonly missing: 'price' | 'time' }
  | { readonly kind: 'no_bar_yet' }
  | { readonly kind: 'not_stored' }
  | { readonly kind: 'unreadable'; readonly problem: string | null }
  | { readonly kind: 'unknown'; readonly code: string };

export function underlyingViewOf(price: UnderlyingPrice): UnderlyingView {
  switch (price.state) {
    case 'priced':
      if (price.price === null) return { kind: 'incomplete', missing: 'price' };
      if (price.price_time_ms === null) return { kind: 'incomplete', missing: 'time' };
      return { kind: 'priced', price: price.price, timeMs: price.price_time_ms };
    case 'no_bar_yet':
      return { kind: 'no_bar_yet' };
    case 'not_stored':
      return { kind: 'not_stored' };
    case 'unreadable':
      return { kind: 'unreadable', problem: price.problem };
    default:
      return { kind: 'unknown', code: String(price.state) };
  }
}

// ---- The download -------------------------------------------------------------------------------

/** What a download would fetch. `underlying` is `unknown` while no chain has been read: the server then looks at it itself. */
export interface DownloadPlan {
  readonly listing: boolean;
  readonly underlying: 'missing' | 'unknown' | 'held';
  /** The OCC symbols of the contracts on screen whose prices are not stored, as the server served them. */
  readonly contracts: readonly string[];
}

/** The plan for what is on screen, or null when nothing on screen is missing. */
export function downloadPlan(day: ChainDay | null, chain: ChainExpiry | null): DownloadPlan | null {
  if (day === null) return null;
  const listing = day.listing_state === 'not_stored';
  const underlying = chain === null ? 'unknown' : chain.underlying_price.state === 'not_stored' ? 'missing' : 'held';
  const contracts = chain === null ? [] : [...chain.calls.contracts, ...chain.puts.contracts].filter((each) => each.price_state === 'not_stored').map((each) => each.symbol);
  if (!listing && underlying !== 'missing' && contracts.length === 0) return null;
  return { listing, underlying, contracts };
}

/** What the button says it will fetch, in words. */
export function planWords(plan: DownloadPlan): string {
  const parts: string[] = [];
  if (plan.listing) parts.push('the listing');
  if (plan.underlying === 'missing') parts.push("the underlying's bars");
  if (plan.underlying === 'unknown') parts.push("the underlying's bars if they are missing");
  if (plan.contracts.length > 0) parts.push(`${plan.contracts.length} ${plan.contracts.length === 1 ? 'contract' : 'contracts'}`);
  const last = parts.pop();
  if (last === undefined) return 'nothing';
  return parts.length === 0 ? last : `${parts.join(', ')} and ${last}`;
}

/** The request for a plan. The expiry rides along only to let the server check the contracts against it. */
export function downloadRequest(plan: DownloadPlan, underlying: string, tradingDateMs: number, expiryMs: number | null): DownloadRequest {
  const withContracts = plan.contracts.length > 0 && expiryMs !== null;
  return withContracts
    ? { underlying, trading_date_ms: tradingDateMs, expiry_ms: expiryMs, contracts: [...plan.contracts] }
    : { underlying, trading_date_ms: tradingDateMs, contracts: [...plan.contracts] };
}

/** Whether the lake refused any item because the session is not over yet. */
export function sessionNotOver(answer: DownloadAnswer): boolean {
  return answer.items.some((each) => each.reason === 'session_not_over');
}

/** How many items of an answer came out each way, for the line above the list. */
export function outcomeCounts(answer: DownloadAnswer): { readonly fetched: number; readonly alreadyStored: number; readonly failed: number } {
  return {
    fetched: answer.items.filter((each) => each.outcome === 'fetched').length,
    alreadyStored: answer.items.filter((each) => each.outcome === 'already_stored').length,
    failed: answer.items.filter((each) => each.outcome === 'failed').length,
  };
}
