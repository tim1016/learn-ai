import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import {
  BEFORE_START_TEXT,
  STRATEGY_RUN_STARTED_AT_MS,
  STRATEGY_RUN_STOPPED_AT_MS,
  barCloseMs,
  fakeStrategyCandle,
  fakeStrategyView,
} from '../../../../testing/strategy-view-fixtures';
import { fakeStrategyChart, type FakeSeries } from '../../../../testing/strategy-chart-fake';
import { formatTimestampDisplay } from '../../../../shared/timestamp/timestamp-display';
import type { StrategyViewResponse } from '../lib/broker-v2-panel.types';
import type { StrategyRunContext } from './chart-lanes';
import type { StrategyChartOverlay } from './strategy-chart-overlay';
import { STRATEGY_CHART_FACTORY, StrategyChartComponent, type StrategyCandleClick } from './strategy-chart.component';
import { GATE_CANDLE_COLORS, toChartTime } from './strategy-view-model';

const markers = vi.hoisted(() => ({ setMarkers: vi.fn() }));

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: vi.fn().mockReturnValue(markers),
  CandlestickSeries: 'CandlestickSeries',
  HistogramSeries: 'HistogramSeries',
  LineSeries: 'LineSeries',
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

async function renderChart(view: StrategyViewResponse = fakeStrategyView(), gateId = 'g_rule', initialWidth = 800) {
  const mock = fakeStrategyChart(vi, initialWidth);
  const clicks: StrategyCandleClick[] = [];
  const rendered = await render(StrategyChartComponent, {
    inputs: { view, gateId },
    on: { candleClicked: (click: StrategyCandleClick) => clicks.push(click) },
    providers: [{ provide: STRATEGY_CHART_FACTORY, useValue: () => mock.chart }],
  });
  return { ...rendered, ...mock, candles: mock.candles(), clicks };
}

function lastData(series: FakeSeries): Record<string, unknown>[] {
  return series.setData.mock.calls.at(-1)?.[0] ?? [];
}

describe('StrategyChartComponent (#2639)', () => {
  beforeEach(() => markers.setMarkers.mockClear());

  it('fills each candle from the active gate’s recorded result, never from its own arithmetic', async () => {
    const { candles } = await renderChart();

    expect(lastData(candles).map(({ time, color, borderColor }) => ({ time, color, borderColor }))).toEqual([
      // up + holds
      { time: barCloseMs(0) / 1000, color: GATE_CANDLE_COLORS.upBright, borderColor: GATE_CANDLE_COLORS.upBright },
      // down + fails
      { time: barCloseMs(1) / 1000, color: GATE_CANDLE_COLORS.downDark, borderColor: GATE_CANDLE_COLORS.downDarkEdge },
      // up + no result reads as dark
      { time: barCloseMs(2) / 1000, color: GATE_CANDLE_COLORS.upDark, borderColor: GATE_CANDLE_COLORS.upDarkEdge },
      // down + holds
      { time: barCloseMs(3) / 1000, color: GATE_CANDLE_COLORS.downBright, borderColor: GATE_CANDLE_COLORS.downBright },
    ]);
  });

  it('re-shades the same candles when the active gate changes', async () => {
    const { candles, fixture } = await renderChart();

    fixture.componentRef.setInput('gateId', 'g_mine');
    await fixture.whenStable();

    expect(lastData(candles).map(({ color }) => color)).toEqual([
      GATE_CANDLE_COLORS.upDark,
      GATE_CANDLE_COLORS.downBright,
      GATE_CANDLE_COLORS.upBright,
      GATE_CANDLE_COLORS.downDark,
    ]);
  });

  it('hands the overlay the bot’s start and end instants and the before-start shade', async () => {
    const { candles } = await renderChart(fakeStrategyView({ run_stopped_at_ms: STRATEGY_RUN_STOPPED_AT_MS }));
    const overlay = candles.attachPrimitive.mock.calls[0][0] as StrategyChartOverlay;

    const state = overlay.current();
    expect(state.shadeBeforeMs).toBe(STRATEGY_RUN_STARTED_AT_MS);
    expect(state.shadeLabel).toBe(BEFORE_START_TEXT);
    expect(state.lines).toEqual([
      {
        atMs: STRATEGY_RUN_STARTED_AT_MS,
        label: `Bot started ${formatTimestampDisplay(STRATEGY_RUN_STARTED_AT_MS, { mode: 'local', granularity: 'minute' })}`,
        emphasis: 'start',
      },
      {
        atMs: STRATEGY_RUN_STOPPED_AT_MS,
        label: `Ended ${formatTimestampDisplay(STRATEGY_RUN_STOPPED_AT_MS, { mode: 'local', granularity: 'minute' })}`,
        emphasis: 'end',
      },
    ]);
    expect(state.bars[0]).toEqual({ startMs: barCloseMs(0) - 15 * 60_000, closeMs: barCloseMs(0) });
  });

  it('shades nothing when the run has no before-start bars', async () => {
    const decided = fakeStrategyView();
    const { candles: decidedCandles } = await renderChart({
      ...decided, candles: decided.candles.filter((candle) => candle.phase === 'decision'),
    });
    expect((decidedCandles.attachPrimitive.mock.calls[0][0] as StrategyChartOverlay).current().shadeBeforeMs).toBeNull();
  });

  it('draws every declared value with a pane from the bot’s values, on the panes the backend names', async () => {
    const { series } = await renderChart();
    const lines = series.filter((each) => each.type === 'LineSeries');

    // "Foo 7" overlays the candles, "Bar 3" gets its own pane, "Baz 1" (no pane) is not drawn.
    expect(lines.map((line) => line.pane)).toEqual([0, 1]);
    const [foo, bar] = lines;
    // The bar whose value was not ready is skipped, never drawn as zero.
    expect(lastData(foo)).toEqual([
      { time: barCloseMs(1) / 1000, value: 101 },
      { time: barCloseMs(2) / 1000, value: 102 },
      { time: barCloseMs(3) / 1000, value: 103 },
    ]);
    expect(bar.createPriceLine.mock.calls.map(([line]) => line.price)).toEqual([20, 80]);
    expect(bar.createPriceLine.mock.calls[0][0]).toMatchObject({ lineStyle: 2 });
  });

  it('re-reads the lines’ points on a refresh with the same declaration, never rebuilding the panes', async () => {
    const { chart, series, fixture } = await renderChart();
    const [foo] = series.filter((each) => each.type === 'LineSeries');
    chart.addSeries.mockClear();

    const view = fakeStrategyView();
    fixture.componentRef.setInput('view', { ...view, candles: [...view.candles, fakeStrategyCandle(4)] });
    await fixture.whenStable();

    expect(chart.addSeries).not.toHaveBeenCalled();
    expect(chart.removeSeries).not.toHaveBeenCalled();
    expect(lastData(foo).at(-1)).toEqual({ time: barCloseMs(4) / 1000, value: 104 });
  });

  it('draws catalogue lines thinner, on the price pane or on panes after the strategy’s, never rebuilding its own', async () => {
    const { chart, series, fixture } = await renderChart();
    const [ownFoo, ownBar] = series.filter((each) => each.type === 'LineSeries');
    chart.removeSeries.mockClear();

    fixture.componentRef.setInput('indicatorPlans', [
      { id: 'vwap', pane: 'main', type: 'line', color: '#e0c050', points: [{ time: toChartTime(barCloseMs(3)), value: 499 }], referenceLevels: [] },
      { id: 'rsi_20', pane: 'rsi', type: 'line', color: '#a0a0ff', points: [], referenceLevels: [30, 70] },
      { id: 'macd-hist', pane: 'macd', type: 'histogram', color: '#888888', points: [], referenceLevels: [] },
    ]);
    await fixture.whenStable();

    const computed = series.slice(-3);
    // The strategy's own lines hold panes 0 and 1, so the catalogue's own panes start at 2.
    expect(computed.map((each) => [each.type, each.pane, each.options['lineWidth']])).toEqual([
      ['LineSeries', 0, 1],
      ['LineSeries', 2, 1],
      ['HistogramSeries', 3, undefined],
    ]);
    expect(computed[0].setData).toHaveBeenLastCalledWith([{ time: toChartTime(barCloseMs(3)), value: 499 }]);
    expect(computed[1].createPriceLine.mock.calls.map(([line]) => line.price)).toEqual([30, 70]);
    expect(chart.removeSeries).not.toHaveBeenCalled();

    fixture.componentRef.setInput('indicatorPlans', []);
    await fixture.whenStable();
    expect(chart.removeSeries.mock.calls.map(([removed]) => removed)).toEqual(computed);
    expect(chart.removeSeries).not.toHaveBeenCalledWith(ownFoo);
    expect(chart.removeSeries).not.toHaveBeenCalledWith(ownBar);
  });

  it('opens on the run once the chart has measured its width, and keeps the viewer’s zoom after', async () => {
    const { timeScale, fixture } = await renderChart(fakeStrategyView(), 'g_rule', 0);
    expect(timeScale.setVisibleLogicalRange).not.toHaveBeenCalled();

    timeScale.resize(640);
    // Four bars, the first decision at index 2: the run plus the bars before
    // it, with room on the right for the run's end line.
    expect(timeScale.setVisibleLogicalRange).toHaveBeenCalledExactlyOnceWith({ from: 0, to: 6 });

    timeScale.resize(700);
    fixture.componentRef.setInput('view', fakeStrategyView({ notices: ['A refreshed read.'] }));
    await fixture.whenStable();
    expect(timeScale.setVisibleLogicalRange).toHaveBeenCalledTimes(1);
  });

  it('opens on a long warmup’s last session, not every warmup day', async () => {
    const warmup = Array.from({ length: 60 }, (_, index) =>
      fakeStrategyCandle(index, { phase: 'before_start', phase_text: BEFORE_START_TEXT }),
    );
    const decided = fakeStrategyCandle(60, { outcome: 'no_action', reason_code: 'NO_ZAP', decision_seq: 1 });
    const { timeScale } = await renderChart(
      fakeStrategyView({ candles: [...warmup, decided], run_started_at_ms: barCloseMs(59) + 1 }),
      'g_rule',
      640,
    );

    expect(timeScale.setVisibleLogicalRange).toHaveBeenCalledExactlyOnceWith({ from: 34, to: 63 });
  });

  it('brings a selected decision’s candle on screen', async () => {
    const { timeScale, fixture } = await renderChart(fakeStrategyView(), 'g_rule', 640);
    timeScale.setVisibleLogicalRange.mockClear();
    timeScale.getVisibleLogicalRange.mockReturnValue({ from: 0, to: 1 });

    fixture.componentRef.setInput('selectedBarCloseMs', barCloseMs(3));
    await fixture.whenStable();

    expect(timeScale.setVisibleLogicalRange).toHaveBeenCalledExactlyOnceWith({ from: 2.5, to: 3.5 });
  });

  it('marks entry and exit decisions only', async () => {
    await renderChart();

    expect(markers.setMarkers).toHaveBeenLastCalledWith([
      expect.objectContaining({ time: barCloseMs(3) / 1000, shape: 'arrowUp', text: 'Enter' }),
    ]);
  });

  it('lets the keyboard step through the candles and open one’s checks', async () => {
    const user = userEvent.setup();
    const mock = fakeStrategyChart(vi);
    const clicks: StrategyCandleClick[] = [];
    const selections: number[] = [];
    const { fixture } = await render(StrategyChartComponent, {
      inputs: { view: fakeStrategyView(), gateId: 'g_rule' },
      on: {
        candleClicked: (click: StrategyCandleClick) => clicks.push(click),
        candleSelected: (barCloseMs: number) => selections.push(barCloseMs),
      },
      providers: [{ provide: STRATEGY_CHART_FACTORY, useValue: () => mock.chart }],
    });
    const select = async (key: string) => {
      await user.keyboard(key);
      fixture.componentRef.setInput('selectedBarCloseMs', selections.at(-1) ?? null);
      await fixture.whenStable();
    };

    screen.getByRole('group', { name: /decision candles for SPY/ }).focus();
    // With nothing selected, an arrow starts from the newest candle.
    await select('{ArrowRight}');
    await select('{ArrowLeft}');
    await select('{Home}');
    await select('{ArrowLeft}');
    await select('{End}');
    expect(selections).toEqual([barCloseMs(3), barCloseMs(2), barCloseMs(0), barCloseMs(0), barCloseMs(3)]);

    // Presses faster than a render still step one bar each.
    await user.keyboard('{ArrowLeft}{ArrowLeft}');
    expect(selections.slice(-2)).toEqual([barCloseMs(2), barCloseMs(1)]);
    await select('{ArrowRight}');
    await user.keyboard('{Enter}');
    // Placed where the candle is drawn: its close's coordinates inside the chart.
    expect(clicks).toEqual([{ barCloseMs: barCloseMs(2), clientX: 120, clientY: 40 }]);
    // Each move is read out: the bar and what the active gate made of it (bar 2 has no result).
    const barTwo = `${formatTimestampDisplay(barCloseMs(2), { mode: 'local', granularity: 'date' })} `
      + formatTimestampDisplay(barCloseMs(2), { mode: 'local', granularity: 'minute' });
    expect(screen.getByText(`${barTwo} bar: gate no result.`).getAttribute('aria-live')).toBe('polite');
    expect(mock.timeScale.timeToCoordinate).toHaveBeenLastCalledWith(toChartTime(barCloseMs(2)));
  });

  it('reports a click on a candle by its bar close', async () => {
    const { click, clicks } = await renderChart();

    click(barCloseMs(2) / 1000, { point: { x: 40, y: 12 }, sourceEvent: { clientX: 300, clientY: 200 } });
    click(undefined, { point: { x: 900, y: 12 } });

    expect(clicks).toEqual([{ barCloseMs: barCloseMs(2), clientX: 300, clientY: 200 }]);
  });
});

describe('StrategyChartComponent with the bot page’s run facts (#2794)', () => {
  const minute = (ms: number) => formatTimestampDisplay(ms, { mode: 'local', granularity: 'minute' });

  function runContext(overrides: Partial<StrategyRunContext> = {}): StrategyRunContext {
    return { nowMs: null, scheduledEndAtMs: null, fills: [], workingOrders: [], feedEvents: [], ...overrides };
  }

  async function renderWithContext(view: StrategyViewResponse, context: StrategyRunContext | null, width = 800) {
    const mock = fakeStrategyChart(vi, width);
    const rendered = await render(StrategyChartComponent, {
      inputs: { view, gateId: 'g_rule', runContext: context },
      providers: [{ provide: STRATEGY_CHART_FACTORY, useValue: () => mock.chart }],
    });
    // Like the library: bar i is centred on x 100 + 20i.
    mock.timeScale.logicalToCoordinate.mockImplementation((logical: number) => 100 + logical * 20);
    mock.timeScale.resize(width);
    await rendered.fixture.whenStable();
    const overlay = mock.candles().attachPrimitive.mock.calls[0][0] as StrategyChartOverlay;
    return { ...rendered, ...mock, overlay };
  }

  it('draws a running bot’s Now line and forming bar, and an end due inside that bar', async () => {
    const nowMs = barCloseMs(3) + 4 * 60_000;
    const formingClose = barCloseMs(3) + 15 * 60_000;
    const { overlay } = await renderWithContext(
      fakeStrategyView(),
      runContext({ nowMs, scheduledEndAtMs: formingClose - 60_000 }),
    );

    const state = overlay.current();
    expect(state.forming).toEqual({
      startMs: barCloseMs(3), closeMs: formingClose, label: `Forming · decides ${minute(formingClose)}`,
    });
    expect(state.lines.slice(1)).toEqual([
      { atMs: formingClose - 60_000, label: `Ends ${minute(formingClose - 60_000)}`, emphasis: 'end' },
      { atMs: nowMs, label: 'Now', emphasis: 'now' },
    ]);
    expect(state.shadeAfterMs).toBeNull();
  });

  it('shades the market after a stopped run, with no Now line or forming bar', async () => {
    const { overlay } = await renderWithContext(
      fakeStrategyView({ run_stopped_at_ms: STRATEGY_RUN_STOPPED_AT_MS }),
      runContext(),
    );

    expect(overlay.current().shadeAfterMs).toBe(STRATEGY_RUN_STOPPED_AT_MS);
    expect(overlay.current().forming).toBeNull();
    expect(overlay.current().lines.map(({ emphasis }) => emphasis)).toEqual(['start', 'end']);
  });

  it('places each lane’s marks on the candles’ clock', async () => {
    await renderWithContext(fakeStrategyView(), runContext({
      fills: [{ filled_at_ms: barCloseMs(3), side: 'buy', quantity: 1, price: 501, order_ref: 'o-1', event_key: 'e-1' }],
    }));

    const lanes = screen.getByRole('group', { name: 'The run’s events on the chart’s clock' });
    const decisions = within(lanes).getByRole('list', { name: 'Decisions' });
    const enter = within(decisions).getByRole('listitem', { name: `${minute(barCloseMs(3))} · Enter` });
    // Bar 3 closes on its candle's right edge: logical 3.5, x 170.
    expect(enter.style.left).toBe('170px');
    const fill = within(screen.getByRole('list', { name: 'Orders' })).getByRole('listitem');
    expect(fill.style.left).toBe('170px');
    expect(fill.getAttribute('title')).toBe(`${minute(barCloseMs(3))} · Bought 1 @ 501`);
  });

  it('keeps a mark scrolled off the chart for screen readers, without placing it', async () => {
    await renderWithContext(fakeStrategyView(), runContext(), 150);

    const enter = screen.getByRole('listitem', { name: `${minute(barCloseMs(3))} · Enter` });
    expect(enter.className).toContain('strategy-lanes__mark--off');
    expect(enter.style.left).toBe('');
  });

  it('draws no lanes without the bot page’s run facts, as in the Strategy Lab', async () => {
    await renderWithContext(fakeStrategyView(), null);

    expect(screen.queryByRole('group', { name: 'The run’s events on the chart’s clock' })).toBeNull();
  });
});
