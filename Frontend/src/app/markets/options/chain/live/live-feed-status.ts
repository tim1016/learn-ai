import { HttpErrorResponse } from '@angular/common/http';

import type { IbkrChainSnapshot, IbkrConnectionHealth } from '../../../../api/broker-models';
import { pythonFailureMessage } from '../../../../services/python-http-failure';
import { errorMessage } from '../../../../shared/errors/error-message';
import { formatTimestampDisplay } from '../../../../shared/timestamp';
import type { WorkspaceStateContent } from '../../../../shared/workspace/workspace-state';
import { FRAME_STALL_MS } from './live-cross-check';
import type { LiveFeedState, LiveSubscription } from './live-chain-feed.service';

/*
 * What Now · Live IBKR says about its feed, as three lines: the connection, the entitlement and
 * the subscription. Each is built from evidence the page holds and says so; none is inferred
 * from a setting. The connection is the data plane's own account of its IBKR session
 * (`BrokerHealthService`), in the words the data plane wrote for it.
 */

/** The sentence a failed read shows: the data plane's own reason when it gave one, else the transport's. */
export function failureWords(error: unknown): string {
  return error instanceof HttpErrorResponse ? pythonFailureMessage(error) : errorMessage(error);
}

export interface StatusLine {
  readonly text: string;
  readonly tone?: 'warn' | 'bad';
}

/** The state the page's first screen holds when the data plane's IBKR session cannot serve it. */
export type LiveGate = { readonly kind: 'ready' } | { readonly kind: 'blocked'; readonly state: WorkspaceStateContent };

const RECHECK = 'This page asks the data plane again every five seconds. Nothing has been switched for you.';

/** IBKR codes that say a market-data request was refused for want of a subscription (the data plane's own `REQUEST_FAILURE_CODES`, less the ones that name a bad contract). */
const ENTITLEMENT_REFUSAL_CODES: ReadonlySet<number> = new Set([354, 10089, 10090, 10167, 10186]);

export interface ConnectionEvidence {
  /** The data plane's latest answer about its IBKR session; null until the first one, or after a failed check. */
  readonly health: IbkrConnectionHealth | null;
  /** Why the check itself failed (the data plane did not answer), when it did. */
  readonly failure: string | null;
}

/** Whether the data plane's IBKR session can serve a chain now, and if not, the reason it gave. */
export function liveGate({ health, failure }: ConnectionEvidence): LiveGate {
  const unavailable = (why: string, next: string): LiveGate => ({
    kind: 'blocked',
    state: { kind: 'unavailable', happened: 'Now · Live IBKR is unavailable.', why, next },
  });
  if (failure !== null) return unavailable(`The data plane did not answer the IBKR connection check: ${failure}`, RECHECK);
  if (health === null) return { kind: 'blocked', state: { kind: 'checking', happened: 'Asking the data plane about its IBKR session.' } };
  if (health.connected) return { kind: 'ready' };
  const reasons = [health.condition?.summary, health.reason].filter((reason): reason is string => typeof reason === 'string' && reason !== '');
  return unavailable(
    reasons.length > 0 ? reasons.join(' ') : `The data plane reports its IBKR session as ${health.connection_state}.`,
    health.condition?.remediation ?? RECHECK,
  );
}

export function connectionLine({ health, failure }: ConnectionEvidence): StatusLine {
  if (failure !== null) return { text: `Unavailable: the data plane did not answer the connection check (${failure}).`, tone: 'bad' };
  if (health === null) return { text: 'Checking the data plane\'s IBKR session.' };
  const text = health.condition == null
    ? `The data plane reports its IBKR session as ${health.connection_state}.`
    : `${health.condition.title}. ${health.condition.summary}`;
  if (!health.connected) return { text: `Unavailable: ${text}`, tone: 'bad' };
  const severity = health.condition?.severity;
  return severity === 'critical' ? { text, tone: 'bad' } : severity === 'warning' || health.connection_state !== 'connected' ? { text, tone: 'warn' } : { text };
}

export interface FeedEvidence {
  readonly subscription: LiveSubscription | null;
  readonly state: LiveFeedState;
  readonly streamError: string | null;
  readonly live: boolean;
  readonly snapshot: IbkrChainSnapshot | null;
  readonly health: IbkrConnectionHealth | null;
}

const REAL_TIME_ONLY = 'The feed asks IBKR for real-time quotes only and never falls back to delayed ones.';

/** How many contracts of the snapshot have sent any quote or Greek at all. */
function contractsHeard(snapshot: IbkrChainSnapshot): number {
  return snapshot.quotes.filter((quote) => quote.bid !== null || quote.ask !== null || quote.last !== null || quote.greeks_source !== 'none').length;
}

/**
 * IBKR does not report an account's market-data entitlement on this feed, so the line says what
 * is known: the feed asks for real-time quotes only, and how many contracts have sent one. A
 * refusal IBKR last reported to the data plane is quoted as such, since it speaks for the whole
 * data plane's session and not for this chain alone.
 */
export function entitlementLine(evidence: FeedEvidence): StatusLine {
  const { snapshot, health } = evidence;
  const refusal = health?.last_ibkr_code != null && ENTITLEMENT_REFUSAL_CODES.has(health.last_ibkr_code)
    ? ` IBKR's latest message to the data plane, for any request, was ${health.last_ibkr_code}: ${health.last_ibkr_message ?? 'no text'}`
    : '';
  if (snapshot === null) return { text: `Not known until a subscription delivers quotes. ${REAL_TIME_ONLY}${refusal}`, ...(refusal === '' ? {} : { tone: 'warn' as const }) };
  const total = snapshot.quotes.length;
  const heard = contractsHeard(snapshot);
  if (heard === total) return { text: `Real-time quotes have arrived for all ${total} contracts. ${REAL_TIME_ONLY}` };
  if (heard === 0) {
    return { text: `No contract has sent a quote. An account without the real-time option-data subscription gets none. ${REAL_TIME_ONLY}${refusal}`, tone: 'bad' };
  }
  return {
    text: `Real-time quotes have arrived for ${heard} of ${total} contracts. The rest have sent none: they may not have quoted yet, or IBKR may refuse them, and this feed does not say which. ${REAL_TIME_ONLY}${refusal}`,
    tone: 'warn',
  };
}

const atTime = (ms: number): string => formatTimestampDisplay(ms, { mode: 'local', granularity: 'time' });

/** A reason as one sentence: the stream's own words end in a full stop already, or do not. */
const sentence = (text: string): string => (text.endsWith('.') ? text : `${text}.`);

const NOT_STARTED: StatusLine = { text: 'Not started. Nothing is subscribed until you press Start.' };

export function subscriptionLine(evidence: FeedEvidence): StatusLine {
  const { subscription, state, snapshot, live, streamError } = evidence;
  if (subscription === null || state === 'none') return NOT_STARTED;
  const contracts = subscription.strikes.length * 2;
  const strikes = subscription.strikes.length;
  const lines = `${contracts} contracts (${strikes} ${strikes === 1 ? 'strike' : 'strikes'}, calls and puts) and the underlying, ${contracts + 1} of IBKR's shared market-data lines`;
  switch (state) {
    case 'stopped':
      return {
        text: `Paused. The stream is closed and its lines are released. ${snapshot === null ? 'No snapshot had arrived.' : `The snapshot on screen is from ${atTime(snapshot.as_of_ms)} and is stale.`}`,
        tone: 'warn',
      };
    case 'error':
      return {
        text: `Stopped: ${sentence(streamError ?? 'the stream reported an error and gave no reason')}${snapshot === null ? '' : ` The snapshot on screen is from ${atTime(snapshot.as_of_ms)} and is stale.`} The stream is closed and its lines are released. It does not reconnect by itself: Resume subscribes again.`,
        tone: 'bad',
      };
    case 'connecting':
      return { text: `Opening the stream for ${lines}.` };
    case 'open':
      if (snapshot === null) return { text: `Open, waiting for the first snapshot: ${lines}.` };
      return live
        ? { text: `Open: ${lines}. Latest snapshot ${atTime(snapshot.as_of_ms)}.` }
        : { text: `Open, but no snapshot has arrived for over ${FRAME_STALL_MS / 1000} s. The snapshot on screen is from ${atTime(snapshot.as_of_ms)} and is stale.`, tone: 'bad' };
  }
}
