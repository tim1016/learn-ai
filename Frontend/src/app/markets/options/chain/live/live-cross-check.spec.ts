import { describe, expect, it } from 'vitest';

import type { IbkrChainSnapshot, IbkrOptionQuote } from '../../../../api/broker-models';
import { optionExpiryAnchorMs } from '../../../../shared/option-contract/option-expiry-date';
import {
  FRAME_STALL_MS,
  QUOTE_MAX_AGE_MS,
  crossCheckCell,
  crossCheckOutcome,
  crossCheckStatement,
  engineInputOf,
  notComparedWords,
  type CrossCheckContext,
  type EngineAnswer,
  type EngineInput,
} from './live-cross-check';
import { AS_OF_MS, NEAR, ibkrFrame, ibkrQuote, silentQuote } from './testing/fake-ibkr';

function context(changes: Partial<CrossCheckContext> = {}, frame: Partial<IbkrChainSnapshot> = {}): CrossCheckContext {
  return { live: true, snapshot: ibkrFrame(frame), engine: 'analytic_bs', ...changes };
}

function reasonOf(quote: IbkrOptionQuote, ctx = context()): string {
  const ready = engineInputOf(quote, ctx);
  return ready.kind === 'ready' ? 'ready' : ready.why.reason;
}

function answered(quote: IbkrOptionQuote, delta: number, ctx = context()): EngineAnswer {
  const ready = engineInputOf(quote, ctx);
  if (ready.kind !== 'ready') throw new Error('the fixture quote cannot be compared');
  return { kind: 'done', input: ready.input, delta, gamma: 0.01 };
}

describe('which contracts the live cross-check compares', () => {
  it('compares a live, two-sided contract with a delta, a volatility, a current quote and a spot', () => {
    const ready = engineInputOf(ibkrQuote(500, 'C'), context());

    expect(ready).toEqual({
      kind: 'ready',
      ibkrDelta: 0.4,
      input: { strike: 500, right: 'C', mid: 5.1, spot: 500, iv: 0.2, expirationMs: NEAR, engine: 'analytic_bs' },
    });
  });

  it.each([
    ['the stream is not delivering', ibkrQuote(500, 'C'), context({ live: false }), 'stream_not_live'],
    ['IBKR sent no Greeks', silentQuote(500, 'C'), context(), 'no_ibkr_delta'],
    ['IBKR sent Greeks without a delta', ibkrQuote(500, 'C', { delta: null }), context(), 'no_ibkr_delta'],
    ['IBKR sent no volatility', ibkrQuote(500, 'C', { iv: null }), context(), 'no_ibkr_iv'],
    ['IBKR\'s volatility is zero', ibkrQuote(500, 'C', { iv: 0 }), context(), 'no_ibkr_iv'],
    ['there is no bid', ibkrQuote(500, 'C', { bid: null }), context(), 'no_two_sided_quote'],
    ['there is no ask', ibkrQuote(500, 'C', { ask: null }), context(), 'no_two_sided_quote'],
    ['both sides are zero', ibkrQuote(500, 'C', { bid: 0, ask: 0 }), context(), 'no_two_sided_quote'],
    ['IBKR sent no price for the underlying', ibkrQuote(500, 'C'), context({}, { underlying_price: null }), 'no_spot'],
    ['the underlying\'s price is zero', ibkrQuote(500, 'C'), context({}, { underlying_price: 0 }), 'no_spot'],
  ] as const)('does not compare a contract when %s', (_name, quote, ctx, reason) => {
    expect(reasonOf(quote, ctx)).toBe(reason);
  });

  it('does not compare a quote older than the limit at the snapshot clock, and does compare one at the limit', () => {
    expect(reasonOf(ibkrQuote(500, 'C', { ts_ms: AS_OF_MS - QUOTE_MAX_AGE_MS }))).toBe('ready');
    expect(reasonOf(ibkrQuote(500, 'C', { ts_ms: AS_OF_MS - QUOTE_MAX_AGE_MS - 1 }))).toBe('quote_stale');
    expect(reasonOf(ibkrQuote(500, 'C', { ts_ms: AS_OF_MS + 1 }))).toBe('quote_stale');
    const stale = engineInputOf(ibkrQuote(500, 'C', { ts_ms: AS_OF_MS - 41_000 }), context());
    expect(stale).toEqual({ kind: 'not_compared', why: { reason: 'quote_stale', ageMs: 41_000 } });
    expect(notComparedWords({ reason: 'quote_stale', ageMs: 41_000 })).toBe('its quote is 41 s older than the snapshot, past the 5 s limit');
  });

  it('does not compare a contract on its expiry date: the engine has no whole day left and answers with the intrinsic delta', () => {
    const expiryDay = Date.UTC(2099, 0, 15, 15, 0, 0);

    expect(reasonOf(ibkrQuote(500, 'C', { ts_ms: expiryDay - 100 }), context({}, { as_of_ms: expiryDay }))).toBe('expires_today');
    expect(reasonOf(ibkrQuote(500, 'C', { ts_ms: AS_OF_MS - 100 }), context({}, { expiry_ms: optionExpiryAnchorMs('2099-01-06') }))).toBe('ready');
  });

  it('answers the reasons in the order a reader asks them: the stream, then IBKR\'s own figures, then the quote, then the underlying', () => {
    const everythingWrong = silentQuote(500, 'C');

    expect(reasonOf(everythingWrong, context({ live: false }, { underlying_price: null }))).toBe('stream_not_live');
    expect(reasonOf(everythingWrong, context({}, { underlying_price: null }))).toBe('no_ibkr_delta');
  });
});

describe('what a compared contract reads', () => {
  it.each([
    ['agree exactly', 0.4, 'green'],
    ['differ by a hair under the green edge', 0.403, 'green'],
    ['differ past the green edge', 0.41, 'yellow'],
    ['differ past the yellow edge', 0.45, 'red'],
  ] as const)('is %s: green up to 0.005, yellow up to 0.02, red above', (_name, engineDelta, band) => {
    const quote = ibkrQuote(500, 'C');

    const cell = crossCheckCell(quote, context(), answered(quote, engineDelta));

    expect(cell).toMatchObject({ kind: 'compared', band, ibkrDelta: 0.4, engineDelta });
    if (cell.kind === 'compared') expect(cell.gap).toBeCloseTo(Math.abs(0.4 - engineDelta), 12);
  });

  it('compares a put against a negative delta by the same distance', () => {
    const quote = ibkrQuote(500, 'P');

    const cell = crossCheckCell(quote, context(), answered(quote, -0.41));

    expect(cell).toMatchObject({ kind: 'compared', band: 'yellow' });
  });

  it('never turns a missing input into a gap of zero: every unmet gate is a cell that says why and has no gap', () => {
    const cases: [IbkrOptionQuote, CrossCheckContext][] = [
      [silentQuote(500, 'C'), context()],
      [ibkrQuote(500, 'C', { iv: null }), context()],
      [ibkrQuote(500, 'C', { bid: null }), context()],
      [ibkrQuote(500, 'C', { ts_ms: AS_OF_MS - 60_000 }), context()],
      [ibkrQuote(500, 'C'), context({}, { underlying_price: null })],
      [ibkrQuote(500, 'C'), context({ live: false })],
    ];
    for (const [quote, ctx] of cases) {
      const cell = crossCheckCell(quote, ctx, { kind: 'done', input: { strike: 500, right: 'C', mid: 5.1, spot: 500, iv: 0.2, expirationMs: NEAR, engine: 'analytic_bs' }, delta: 0.4, gamma: 0.01 });
      expect.soft(cell.kind).toBe('not_compared');
      expect.soft(JSON.stringify(cell)).not.toContain('"gap"');
    }
  });

  it('waits for the engine rather than show a gap against an answer to a different question', () => {
    const quote = ibkrQuote(500, 'C');
    const old: EngineInput = { strike: 500, right: 'C', mid: 5.1, spot: 499, iv: 0.2, expirationMs: NEAR, engine: 'analytic_bs' };

    expect(crossCheckCell(quote, context(), undefined)).toEqual({ kind: 'not_compared', why: { reason: 'engine_pending' } });
    expect(crossCheckCell(quote, context(), { kind: 'done', input: old, delta: 0.4, gamma: 0 })).toEqual({ kind: 'not_compared', why: { reason: 'engine_pending' } });
    const engineChanged = { ...old, spot: 500, engine: 'binomial_crr' } as const;
    expect(crossCheckCell(quote, context(), { kind: 'done', input: engineChanged, delta: 0.4, gamma: 0 })).toMatchObject({ kind: 'not_compared' });
  });

  it('keeps an answer for a midpoint that moved less than a cent, because the midpoint is not what the engine is valued with', () => {
    const quote = ibkrQuote(500, 'C', { bid: 5, ask: 5.205 });
    const priced: EngineInput = { strike: 500, right: 'C', mid: 5.1, spot: 500, iv: 0.2, expirationMs: NEAR, engine: 'analytic_bs' };

    expect(crossCheckCell(quote, context(), { kind: 'done', input: priced, delta: 0.4, gamma: 0 }).kind).toBe('compared');
  });

  it('says a failed engine read with its reason, and no gap', () => {
    const quote = ibkrQuote(500, 'C');
    const ready = engineInputOf(quote, context());
    if (ready.kind !== 'ready') throw new Error('not ready');

    const cell = crossCheckCell(quote, context(), { kind: 'failed', input: ready.input, why: 'QuantLib not installed' });

    expect(cell).toEqual({ kind: 'not_compared', why: { reason: 'engine_failed', why: 'QuantLib not installed' } });
    expect(notComparedWords({ reason: 'engine_failed', why: 'QuantLib not installed' })).toBe('the engine could not calculate it: QuantLib not installed');
  });
});

describe('what the chain says about what it compared', () => {
  it('counts the contracts compared and says why each of the others was not', () => {
    const good = ibkrQuote(500, 'C');
    const cells = [
      crossCheckCell(good, context(), answered(good, 0.4)),
      crossCheckCell(silentQuote(501, 'C'), context(), undefined),
      crossCheckCell(ibkrQuote(502, 'C', { ts_ms: AS_OF_MS - 60_000 }), context(), undefined),
      crossCheckCell(ibkrQuote(503, 'C', { ts_ms: AS_OF_MS - 60_000 }), context(), undefined),
    ];

    expect(crossCheckOutcome(cells)).toBe('Compared 1 of 4 contracts. Not compared: 1 no IBKR delta; 2 quote older than 5 s.');
  });

  it('says nothing before a contract is on screen, and nothing is left out when all were compared', () => {
    const good = ibkrQuote(500, 'C');

    expect(crossCheckOutcome([])).toBeNull();
    expect(crossCheckOutcome([crossCheckCell(good, context(), answered(good, 0.4))])).toBe('Compared 1 of 1 contracts.');
  });

  it('states the quantity, its units, each side, the inputs, the timestamps and the tolerance the columns are held to', () => {
    const statement = crossCheckStatement('analytic_bs', null);
    const said = (label: string) => statement.statements.find((each) => each.label === label)?.text ?? '';

    expect(statement.statements.map((each) => each.label)).toEqual(['Quantity', 'Δ IBKR', 'Δ Engine', '|ΔΔ|', 'Valued with', 'As of', 'Not compared']);
    expect(said('Quantity')).toMatch(/Delta .* \$1 move .* no unit/);
    expect(said('Δ Engine')).toContain('analytic_bs');
    expect(said('|ΔΔ|')).toMatch(/delta units.*Green up to 0\.005, yellow up to 0\.02, red above/);
    expect(said('|ΔΔ|')).toContain('never a gap of zero');
    for (const input of ['strike', 'underlying', 'implied volatility', '16:00 New York', 'valuation date', 'risk-free rate', 'dividend yield']) {
      expect.soft(said('Valued with'), input).toContain(input);
    }
    expect(said('As of')).toContain(`${QUOTE_MAX_AGE_MS / 1000} s`);
    expect(said('As of')).toContain(`${FRAME_STALL_MS / 1000} s`);
    expect(said('Not compared')).toContain('expiry date');
  });
});
