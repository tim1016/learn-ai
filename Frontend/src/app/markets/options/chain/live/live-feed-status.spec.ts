import { HttpErrorResponse } from '@angular/common/http';
import { describe, expect, it } from 'vitest';

import { connectionLine, entitlementLine, failureWords, liveGate, subscriptionLine, type FeedEvidence } from './live-feed-status';
import { AS_OF_MS, connectedHealth, disconnectedHealth, ibkrFrame, ibkrQuote, silentQuote } from './testing/fake-ibkr';

describe('whether the data plane\'s IBKR session can serve a live chain', () => {
  it('serves when the session is connected', () => {
    expect(liveGate({ health: connectedHealth(), failure: null })).toEqual({ kind: 'ready' });
  });

  it('is unavailable with the data plane\'s own words when it holds no Gateway session', () => {
    const gate = liveGate({ health: disconnectedHealth(), failure: null });

    expect(gate).toMatchObject({
      kind: 'blocked',
      state: {
        kind: 'unavailable',
        happened: 'Now · Live IBKR is unavailable.',
        why: expect.stringContaining('The FastAPI data-plane IBKR client is disconnected.'),
        next: 'Use the IBKR Connect control to establish the data-plane session.',
      },
    });
  });

  it('adds the reason a switched-off broker gives to its summary', () => {
    const gate = liveGate({
      health: disconnectedHealth({
        disabled: true,
        connection_state: 'disabled',
        reason: 'IBKR_BROKER_ENABLED=false — the data-plane IBKR client is switched off',
        condition: { code: 'DATA_PLANE_BROKER_DISABLED', severity: 'info', title: 'Data-plane broker disabled', summary: 'The FastAPI data-plane broker client is disabled for this process.' },
      }),
      failure: null,
    });

    expect(gate).toMatchObject({ kind: 'blocked', state: { kind: 'unavailable', why: 'The FastAPI data-plane broker client is disabled for this process. IBKR_BROKER_ENABLED=false — the data-plane IBKR client is switched off' } });
  });

  it('names the connection state when the data plane gave no words, and says it asks again', () => {
    const gate = liveGate({ health: disconnectedHealth({ condition: null }), failure: null });

    expect(gate).toMatchObject({ kind: 'blocked', state: { kind: 'unavailable', why: 'The data plane reports its IBKR session as disconnected.', next: expect.stringContaining('asks the data plane again') } });
  });

  it('is unavailable, not checking and not connected, when the data plane did not answer', () => {
    const gate = liveGate({ health: null, failure: 'Http failure response for /api/broker/health: 0 Unknown Error' });

    expect(gate).toMatchObject({ kind: 'blocked', state: { kind: 'unavailable', why: 'The data plane did not answer the IBKR connection check: Http failure response for /api/broker/health: 0 Unknown Error' } });
  });

  it('is only checking before the first answer', () => {
    expect(liveGate({ health: null, failure: null })).toMatchObject({ kind: 'blocked', state: { kind: 'checking' } });
  });

  it('reads a failed call by the data plane\'s own reason, not the transport\'s', () => {
    const refused = new HttpErrorResponse({ status: 503, error: { detail: 'IBKR client not connected to Gateway.' } });

    expect(failureWords(refused)).toBe('IBKR client not connected to Gateway.');
    expect(failureWords(new Error('boom'))).toBe('boom');
  });
});

describe('the connection line', () => {
  it('quotes the data plane\'s title and summary for a healthy session, with no warning', () => {
    expect(connectionLine({ health: connectedHealth(), failure: null })).toEqual({
      text: 'Data-plane paper session connected. The FastAPI data-plane IBKR client is connected. Market data can stream for this session.',
    });
  });

  it('warns while the session is up but its feed is not whole, and calls a critical one bad', () => {
    const degraded = connectedHealth({
      connection_state: 'subscriptions_stale',
      condition: { code: 'DATA_PLANE_BROKER_SUBSCRIPTIONS_STALE', severity: 'warning', title: 'Data-plane subscriptions stale', summary: 'Subscriptions need refresh after recovery.' },
    });
    const critical = connectedHealth({
      connection_state: 'degraded_data_farm',
      condition: { code: 'DATA_PLANE_BROKER_DATA_FARM_DEGRADED', severity: 'critical', title: 'IBKR data farm degraded', summary: 'Do not rely on market-data freshness.' },
    });

    expect(connectionLine({ health: degraded, failure: null }).tone).toBe('warn');
    expect(connectionLine({ health: critical, failure: null }).tone).toBe('bad');
  });

  it('says unavailable when there is no session or no answer', () => {
    expect(connectionLine({ health: disconnectedHealth(), failure: null })).toMatchObject({ tone: 'bad', text: expect.stringMatching(/^Unavailable: Data-plane broker session disconnected\./) });
    expect(connectionLine({ health: null, failure: 'no answer' })).toMatchObject({ tone: 'bad', text: expect.stringMatching(/^Unavailable: .*no answer/) });
    expect(connectionLine({ health: null, failure: null })).toEqual({ text: 'Checking the data plane\'s IBKR session.' });
  });
});

function feed(changes: Partial<FeedEvidence> = {}): FeedEvidence {
  return { subscription: null, state: 'none', streamError: null, live: false, snapshot: null, health: connectedHealth(), ...changes };
}

describe('the entitlement line', () => {
  it('says what is known before a subscription delivers: real-time quotes are asked for, and never delayed ones', () => {
    expect(entitlementLine(feed()).text).toBe('Not known until a subscription delivers quotes. The feed asks IBKR for real-time quotes only and never falls back to delayed ones.');
  });

  it('counts the contracts that have sent a quote, and warns when some have not', () => {
    const all = ibkrFrame();
    const some = ibkrFrame({ quotes: [ibkrQuote(500, 'C'), silentQuote(500, 'P')] });
    const none = ibkrFrame({ quotes: [silentQuote(500, 'C'), silentQuote(500, 'P')] });

    expect(entitlementLine(feed({ snapshot: all }))).toMatchObject({ text: expect.stringContaining('arrived for all 2 contracts') });
    expect(entitlementLine(feed({ snapshot: all })).tone).toBeUndefined();
    expect(entitlementLine(feed({ snapshot: some }))).toMatchObject({ tone: 'warn', text: expect.stringContaining('arrived for 1 of 2 contracts') });
    expect(entitlementLine(feed({ snapshot: none }))).toMatchObject({ tone: 'bad', text: expect.stringContaining('No contract has sent a quote') });
  });

  it('quotes a refusal IBKR last reported to the data plane as that, for any request', () => {
    const refused = connectedHealth({ last_ibkr_code: 10089, last_ibkr_message: 'Requested market data requires additional subscription for API.' });
    const informational = connectedHealth({ last_ibkr_code: 2104, last_ibkr_message: 'Market data farm connection is OK' });

    const line = entitlementLine(feed({ health: refused, snapshot: ibkrFrame({ quotes: [silentQuote(500, 'C')] }) }));

    expect(line.text).toContain('IBKR\'s latest message to the data plane, for any request, was 10089: Requested market data requires additional subscription for API.');
    expect(entitlementLine(feed({ health: informational, snapshot: ibkrFrame({ quotes: [silentQuote(500, 'C')] }) })).text).not.toContain('2104');
  });
});

describe('the subscription line', () => {
  const held = { symbol: 'SPY', expiryMs: 1, strikes: [499, 500, 501] };

  it('says nothing is subscribed until Start is pressed', () => {
    expect(subscriptionLine(feed()).text).toBe('Not started. Nothing is subscribed until you press Start.');
  });

  it('counts the contracts and the lines a subscription holds', () => {
    expect(subscriptionLine(feed({ subscription: held, state: 'connecting' })).text).toBe('Opening the stream for 6 contracts (3 strikes, calls and puts) and the underlying, 7 of IBKR\'s shared market-data lines.');
    expect(subscriptionLine(feed({ subscription: held, state: 'open' })).text).toContain('Open, waiting for the first snapshot: 6 contracts');
    expect(subscriptionLine(feed({ subscription: held, state: 'open', snapshot: ibkrFrame(), live: true }))).toEqual({
      text: expect.stringMatching(/^Open: 6 contracts .* Latest snapshot \d{2}:\d{2}:\d{2}\.$/),
    });
  });

  it('calls a snapshot stale when the stream is paused, stopped, or silent', () => {
    const snapshot = ibkrFrame({ as_of_ms: AS_OF_MS });

    expect(subscriptionLine(feed({ subscription: held, state: 'stopped', snapshot }))).toMatchObject({ tone: 'warn', text: expect.stringMatching(/^Paused\. The stream is closed and its lines are released\..* is stale\.$/) });
    expect(subscriptionLine(feed({ subscription: held, state: 'error', snapshot, streamError: 'The option-chain stream stopped.' }))).toMatchObject({ tone: 'bad', text: expect.stringMatching(/^Stopped: The option-chain stream stopped\. The snapshot on screen is from .* and is stale\.$/) });
    expect(subscriptionLine(feed({ subscription: held, state: 'open', snapshot, live: false }))).toMatchObject({ tone: 'bad', text: expect.stringContaining('no snapshot has arrived for over 5 s') });
  });
});
