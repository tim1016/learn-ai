import type { ResourceStatus } from '@angular/core';

import { NOT_A_SESSION, type ControlName, type ReadProblem } from './past-chain-problems';
import type { ChainContract, ChainDay, ChainExpiry, ChainSide, DownloadAnswer, DownloadRequest, SessionBounds, UnderlyingPrice } from './past-chain.types';

/**
 * What the tab shows, decided from the server's state codes and from nothing else. Each state the brief names is a
 * kind here, so that a template cannot forget one and a spec can name it. Nothing in this file prices a contract,
 * builds a symbol, or does arithmetic on a served figure: strikes are compared, counts are counted.
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
  if (day.listing_state === 'not_stored') return { kind: 'not_stored' };
  if (day.listing_state === 'unreadable') return { kind: 'unreadable', problem: day.listing_problem };
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
  if (read === 'idle' || read === 'loading') return { kind: 'loading' };
  const chain = inputs.chain;
  if (read === 'error' || chain === null) {
    const problem = inputs.problem;
    if (problem === null) return { kind: 'failed', message: 'The chain could not be read.', code: null, canRetry: true };
    // A refusal about a control is said at that control; the table has nothing to show meanwhile.
    return problem.control === 'area' ? { kind: 'failed', message: problem.message, code: problem.code, canRetry: problem.canRetry } : { kind: 'blocked', control: problem.control };
  }
  if (chain.expiry_listed === false) return { kind: 'not_listed' };
  if (chain.expiry_listed === null) {
    return { kind: 'listing_unknown', state: chain.listing_state === 'unreadable' ? 'unreadable' : 'not_stored', problem: chain.listing_problem };
  }
  return { kind: 'shown', chain };
}

// ---- The table ----------------------------------------------------------------------------------

/** One strike of the table: the call and the put listed at it (either may be absent), and where each side's money is. */
export interface StrikeRow {
  readonly thousandths: number;
  /** The strike in dollars, as the server wrote it. */
  readonly strike: string;
  readonly call: ChainContract | null;
  readonly put: ChainContract | null;
  /** This is the strike the calls' money was found at; likewise the puts'. The two sides' ladders can differ. */
  readonly callMoney: boolean;
  readonly putMoney: boolean;
}

/**
 * The strikes of both sides, ascending, one row each. Each side arrives ascending by strike, so the two lists are merged
 * by comparing their strikes: nothing is subtracted, and a strike listed on one side only is a row with the other side empty.
 */
export function strikeRows(calls: ChainSide, puts: ChainSide): StrikeRow[] {
  const rows: StrikeRow[] = [];
  let c = 0;
  let p = 0;
  for (;;) {
    const call = calls.contracts.at(c) ?? null;
    const put = puts.contracts.at(p) ?? null;
    if (call !== null && (put === null || call.strike_thousandths <= put.strike_thousandths)) {
      const paired = put !== null && put.strike_thousandths === call.strike_thousandths;
      rows.push({
        thousandths: call.strike_thousandths,
        strike: call.strike,
        call,
        put: paired ? put : null,
        callMoney: calls.at_the_money_strike_thousandths === call.strike_thousandths,
        putMoney: paired && puts.at_the_money_strike_thousandths === call.strike_thousandths,
      });
      c += 1;
      if (paired) p += 1;
    } else if (put !== null) {
      rows.push({ thousandths: put.strike_thousandths, strike: put.strike, call: null, put, callMoney: false, putMoney: puts.at_the_money_strike_thousandths === put.strike_thousandths });
      p += 1;
    } else {
      return rows;
    }
  }
}

/** What one contract's cell says. A price is only ever a `traded` cell, and a `traded` cell always carries its time. */
export type CellView =
  | { readonly kind: 'traded'; readonly price: string; readonly timeMs: number }
  /** The server said it traded and left out the price or the time of the trade: neither is guessed, and no price is drawn. */
  | { readonly kind: 'incomplete'; readonly missing: 'price' | 'time' }
  | { readonly kind: 'did_not_trade' }
  | { readonly kind: 'not_stored' }
  | { readonly kind: 'unreadable'; readonly problem: string | null };

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
  }
}

/** The underlying's own price at the instant, by the same rule: a price only with its time. */
export type UnderlyingView =
  | { readonly kind: 'priced'; readonly price: string; readonly timeMs: number }
  | { readonly kind: 'incomplete'; readonly missing: 'price' | 'time' }
  | { readonly kind: 'no_bar_yet' }
  | { readonly kind: 'not_stored' }
  | { readonly kind: 'unreadable'; readonly problem: string | null };

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
