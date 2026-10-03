import { describe, expect, it } from 'vitest';

import {
  FAKE_GATE_CATALOGUE,
  barCloseMs,
  fakeCustomGate,
  fakeLeadInBar,
  fakeStrategyView,
} from '../../../../testing/strategy-view-fixtures';
import { DRAFT_GATE_ID, gateEvaluationRequest, gateVariableGroups, withCustomGates } from './strategy-gates-model';

describe('strategy gates model (#2639)', () => {
  it('asks the data plane about every candle with the bot’s values by key and the deployed settings', () => {
    const request = gateEvaluationRequest(fakeStrategyView(), { label: 'Mine', expression: 'FOO7', sign: 'lt' });

    expect(request.symbol).toBe('SPY');
    expect(request.settings).toEqual({ foo_length: 7, bar_low: 20, fast: true });
    expect(request.draft).toEqual({ label: 'Mine', expression: 'FOO7', sign: 'lt' });
    // The first bar's Foo 7 was not ready: sent as no value, never as zero.
    expect(request.candles.map((candle) => candle.values?.['foo'])).toEqual([null, 101, 102, 103]);
    expect(request.candles[1]).toEqual({
      bar_close_ms: barCloseMs(1), open: 502, high: 503, low: 498, close: 499, volume: 1_000,
      values: { foo: 101, bar: 51, baz: 1 },
    });
  });

  it('sends the view’s lead-in bars beside the candles, for catalogue indicators to warm up on (#2800)', () => {
    const leadIn = [fakeLeadInBar(2), fakeLeadInBar(1)];

    const request = gateEvaluationRequest(fakeStrategyView({ lead_in: leadIn }));

    expect(request.lead_in).toEqual(leadIn);
    // The candles judged are still only the view's own.
    expect(request.candles.map((candle) => candle.bar_close_ms)).toEqual([0, 1, 2, 3].map(barCloseMs));
    // A bot's view carries none.
    expect(gateEvaluationRequest(fakeStrategyView()).lead_in).toEqual([]);
  });

  it('lists saved gates and a draft after the strategy’s own, each recorded on the candle it was judged on', () => {
    const view = fakeStrategyView();
    const gate = fakeCustomGate();
    const shown = withCustomGates(
      view,
      [gate],
      [
        // Judged on an earlier read: bar 3 was not yet drawn, and a bar no longer drawn is ignored.
        {
          closes: [barCloseMs(-1), barCloseMs(0), barCloseMs(1), barCloseMs(2)],
          response: { results: { [gate.gate_id]: [true, false, true, true] }, chart_computed: [], notices: ['Saw it.'] },
        },
        {
          closes: view.candles.map((candle) => candle.bar_close_ms),
          response: { results: { [DRAFT_GATE_ID]: [true, true, false, null] }, chart_computed: [], notices: [] },
        },
      ],
      { label: 'Rising', expression: 'FOO7 - close', sign: 'lt' },
      ['A notice of the page’s own.'],
    );

    expect(shown.declaration.gates.map(({ gate_id, label, expression, source }) => ({ gate_id, label, expression, source })))
      .toEqual([
        ...view.declaration.gates,
        { gate_id: gate.gate_id, label: 'Foo above close', expression: 'FOO7 - close > 0', source: 'mine' },
        { gate_id: DRAFT_GATE_ID, label: 'Preview · Rising', expression: 'FOO7 - close < 0', source: 'mine' },
      ]);
    expect(shown.candles.map((candle) => candle.gates[gate.gate_id] ?? 'none')).toEqual([false, true, true, 'none']);
    expect(shown.candles.map((candle) => candle.gates[DRAFT_GATE_ID])).toEqual([true, true, false, null]);
    // The strategy's own results are untouched.
    expect(shown.candles.map((candle) => candle.gates['g_rule'])).toEqual(view.candles.map((candle) => candle.gates['g_rule']));
    expect(shown.notices).toEqual(['Saw it.', 'A notice of the page’s own.']);
  });

  it('offers names in the order the data plane resolves them, the catalogue as the data plane offers it', () => {
    const groups = gateVariableGroups(fakeStrategyView(), FAKE_GATE_CATALOGUE);

    expect(groups.map((group) => [group.title, group.chips.map((chip) => chip.name)])).toEqual([
      ['Bot’s values', ['FOO7', 'BAR3', 'BAZ1']],
      // A setting that is not a number cannot be read by a linear gate.
      ['Settings', ['foo_length', 'bar_low']],
      ['Candle', ['open', 'high', 'low', 'close', 'volume']],
      ['Catalogue', ['EMA10', 'VWAP']],
    ]);
    expect(groups.find((group) => group.title === 'Catalogue')?.note).toBe('chart-computed from these candles');
  });

  it('leaves out a catalogue name the bot already records, which the data plane reads as the bot’s value', () => {
    const view = fakeStrategyView();
    const recordsEma = fakeStrategyView({
      declaration: {
        ...view.declaration,
        values: [
          ...view.declaration.values,
          { key: 'ema_slow', label: 'EMA 10', variable: 'EMA10', pane: 'price', band: null, decimals: 2, catalogue: null },
        ],
      },
    });

    const catalogue = gateVariableGroups(recordsEma, FAKE_GATE_CATALOGUE).find((group) => group.title === 'Catalogue');
    expect(catalogue?.chips.map((chip) => chip.name)).toEqual(['VWAP']);
  });
});
