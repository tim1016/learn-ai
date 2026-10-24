import { describe, expect, it } from 'vitest';

import type { ChainContractCell, ChainField, ChainPresentation, ChainRow } from '../chain-presentation';
import { contractKey, engineInputOf, type EngineAnswer } from './live-cross-check';
import { livePresentation, type LiveChainInputs } from './live-chain';
import { AS_OF_MS, NEAR, ibkrFrame, ibkrQuote, silentQuote } from './testing/fake-ibkr';

function inputs(changes: Partial<LiveChainInputs> = {}): LiveChainInputs {
  return {
    symbol: 'SPY',
    expiryMs: NEAR,
    snapshot: ibkrFrame(),
    serving: true,
    live: true,
    engine: 'analytic_bs',
    answers: new Map(),
    connection: { text: 'Connected.' },
    entitlement: { text: 'Quotes arriving.' },
    subscription: { text: 'Open.' },
    ...changes,
  };
}

/** The engine's answers to every contract of the default frame, at the deltas given. */
function answers(call: number, put: number, frame = ibkrFrame()): ReadonlyMap<string, EngineAnswer> {
  const map = new Map<string, EngineAnswer>();
  for (const quote of frame.quotes) {
    const ready = engineInputOf(quote, { live: true, snapshot: frame, engine: 'analytic_bs' });
    if (ready.kind === 'ready') map.set(contractKey(quote.strike, quote.right), { kind: 'done', input: ready.input, delta: quote.right === 'C' ? call : put, gamma: 0 });
  }
  return map;
}

const contract = (side: ChainRow['call']): ChainContractCell => {
  if (side.kind !== 'contract') throw new Error('the side is a gap');
  return side;
};
const text = (field: ChainField): string => (field.kind === 'value' ? field.text : `absent:${field.reason}`);
const row = (presentation: ChainPresentation, strike: number): ChainRow => {
  const found = presentation.table.rows.find((each) => each.key === String(strike));
  if (found === undefined) throw new Error(`no row at ${strike}`);
  return found;
};
const fact = (presentation: ChainPresentation, label: string) => presentation.facts.find((each) => each.label === label);

describe('Now · Live IBKR, as the one chain presentation', () => {
  it('draws the Chain (Live) page\'s columns mirrored about the strike, with headers that say whose figure each is', () => {
    const { columns, centre } = livePresentation(inputs()).table;

    expect(columns.calls).toEqual(['deltaGap', 'engineDelta', 'delta', 'iv', 'bid', 'ask']);
    expect(columns.puts).toEqual(['bid', 'ask', 'iv', 'delta', 'engineDelta', 'deltaGap']);
    expect(centre).toBe(false);
    expect(columns.headers?.delta?.label).toBe('Δ IBKR');
    expect(columns.headers?.engineDelta?.label).toBe('Δ Engine');
    expect(columns.headers?.deltaGap).toEqual({ label: '|ΔΔ|', title: expect.stringContaining('never zero') });
  });

  it('draws what IBKR sent for a contract, each figure with the time of the quote it came from', () => {
    const call = contract(row(livePresentation(inputs()), 500).call);

    expect(call.fields.bid).toEqual({ kind: 'value', text: '5.00', atMs: AS_OF_MS - 100 });
    expect(call.fields.ask).toEqual({ kind: 'value', text: '5.20', atMs: AS_OF_MS - 100 });
    expect(call.fields.iv).toEqual({ kind: 'value', text: '20.00%', atMs: AS_OF_MS - 100 });
    expect(call.fields.delta).toEqual({ kind: 'value', text: '0.400', atMs: AS_OF_MS - 100 });
  });

  it('says why a figure IBKR has not sent is absent, and that volume and open interest are not on this feed', () => {
    const presentation = livePresentation(inputs({ snapshot: ibkrFrame({ quotes: [silentQuote(500, 'C'), ibkrQuote(500, 'P')] }) }));

    const call = contract(row(presentation, 500).call);

    expect(text(call.fields.bid)).toBe('absent:not_sent_yet');
    expect(text(call.fields.delta)).toBe('absent:not_sent_yet');
    expect(text(call.fields.volume)).toBe('absent:not_on_feed');
    expect(text(call.fields.openInterest)).toBe('absent:not_on_feed');
  });

  it('draws the engine\'s delta and the gap in its band where the contract was compared', () => {
    const presentation = livePresentation(inputs({ answers: answers(0.41, -0.4) }));

    const call = contract(row(presentation, 500).call);
    const put = contract(row(presentation, 500).put);

    expect(call.fields.engineDelta).toEqual({ kind: 'value', text: '0.410', atMs: null });
    expect(call.fields.deltaGap).toMatchObject({ kind: 'value', text: '0.0100', band: 'yellow' });
    expect(put.fields.deltaGap).toMatchObject({ kind: 'value', text: '0.0000', band: 'green' });
  });

  it('says unavailable, with the reason, where a contract was not compared, and gives it no band', () => {
    const frame = ibkrFrame({ quotes: [ibkrQuote(500, 'C', { ts_ms: AS_OF_MS - 41_000 }), ibkrQuote(500, 'P')] });
    const presentation = livePresentation(inputs({ snapshot: frame, answers: answers(0.4, -0.4, frame) }));

    const stale = contract(row(presentation, 500).call);

    for (const column of ['engineDelta', 'deltaGap'] as const) {
      expect.soft(stale.fields[column], column).toEqual({
        kind: 'absent',
        reason: 'not_compared',
        detail: 'its quote is 41 s older than the snapshot, past the 5 s limit',
        shown: 'unavailable',
      });
    }
    expect(contract(row(presentation, 500).put).fields.deltaGap.kind).toBe('value');
  });

  it('draws nothing as compared before the engine has answered', () => {
    const call = contract(row(livePresentation(inputs()), 500).call);

    expect(call.fields.deltaGap).toMatchObject({ kind: 'absent', detail: 'the engine has not answered yet', shown: 'unavailable' });
  });

  it('marks the strike nearest the underlying\'s price, and shades the side in the money', () => {
    const frame = ibkrFrame({ underlying_price: 500.4, quotes: [499, 500, 501].flatMap((strike) => [ibkrQuote(strike, 'C'), ibkrQuote(strike, 'P')]) });

    const presentation = livePresentation(inputs({ snapshot: frame }));

    expect(presentation.table.rows.map((each) => each.strike)).toEqual(['499.00', '500.00', '501.00']);
    expect(row(presentation, 500)).toMatchObject({ atm: true, mark: 'ATM 500.40', itm: null });
    expect(row(presentation, 499)).toMatchObject({ atm: false, itm: 'call' });
    expect(row(presentation, 501)).toMatchObject({ atm: false, itm: 'put' });
  });

  it('shades and marks nothing where IBKR sent no price for the underlying', () => {
    const presentation = livePresentation(inputs({ snapshot: ibkrFrame({ underlying_price: null }) }));

    expect(row(presentation, 500)).toMatchObject({ atm: false, mark: null, itm: null });
    expect(fact(presentation, 'Underlying')?.value).toEqual({ kind: 'text', text: 'SPY —: IBKR has sent no price for it' });
  });

  it('says a strike has no contract on a side rather than draw a blank', () => {
    const presentation = livePresentation(inputs({ snapshot: ibkrFrame({ quotes: [ibkrQuote(500, 'C')] }) }));

    expect(row(presentation, 500).put).toEqual({ kind: 'gap', words: 'No contract at this strike' });
  });

  it('states connection, entitlement and subscription as facts, with their tone', () => {
    const presentation = livePresentation(inputs({ connection: { text: 'Unavailable: down.', tone: 'bad' }, subscription: { text: 'Paused.', tone: 'warn' } }));

    expect(fact(presentation, 'Connection')).toEqual({ label: 'Connection', value: { kind: 'text', text: 'Unavailable: down.' }, tone: 'bad' });
    expect(fact(presentation, 'Entitlement')?.value).toEqual({ kind: 'text', text: 'Quotes arriving.' });
    expect(fact(presentation, 'Subscription')?.tone).toBe('warn');
    expect(fact(presentation, 'Source')?.value).toMatchObject({ text: expect.stringContaining('Real-time quotes only') });
  });

  it('dates the snapshot and names the expiry once there is a chain, and neither before', () => {
    const before = livePresentation(inputs({ snapshot: null, expiryMs: null }));
    const after = livePresentation(inputs());

    expect(before.facts.map((each) => each.label)).toEqual(['Source', 'Connection', 'Entitlement', 'Subscription']);
    expect(before.table.rows).toEqual([]);
    expect(fact(after, 'Snapshot')?.value).toEqual({ kind: 'instant', ms: AS_OF_MS, mode: 'local', granularity: 'time' });
    expect(fact(after, 'Expiry')?.value).toEqual({ kind: 'expiry', ms: NEAR, tag: null });
  });

  it('prints what the columns compare, with how many contracts were, while IBKR can serve, and not when it cannot', () => {
    const serving = livePresentation(inputs({ answers: answers(0.4, -0.4) }));
    const down = livePresentation(inputs({ serving: false, snapshot: null }));

    expect(serving.crossCheck?.outcome).toBe('Compared 2 of 2 contracts.');
    expect(serving.crossCheck?.statements.map((each) => each.label)).toContain('Valued with');
    expect(down.crossCheck).toBeUndefined();
  });

  it('compares no contract while the stream is not live: the last snapshot is stale', () => {
    const presentation = livePresentation(inputs({ live: false, answers: answers(0.4, -0.4) }));

    expect(presentation.crossCheck?.outcome).toBe('Compared 0 of 2 contracts. Not compared: 2 stream not live.');
    expect(contract(row(presentation, 500).call).fields.deltaGap).toMatchObject({ kind: 'absent', shown: 'unavailable' });
  });
});
