import { render, screen, waitFor } from '@testing-library/angular';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { SessionDetail } from '../run-detail.types';
import { buildTimelinePlan } from '../timeline-plan';
import { at, CALL_585, PUT_585, sessionDetail, shownSeries, skippedSession } from '../testing/detail-fixtures';
import { fakeTimelineChart, type FakeTimelineChart } from '../testing/timeline-chart-fake';
import { TIMELINE_CHART_FACTORY, TIMELINE_COLOURS, TimelineChartComponent } from './timeline-chart.component';

const markers = vi.hoisted(() => ({ create: vi.fn() }));

vi.mock('lightweight-charts', () => ({
  createSeriesMarkers: markers.create,
  LineSeries: 'LineSeries',
  LineStyle: { Solid: 0, Dotted: 1, Dashed: 2, LargeDashed: 3, SparseDotted: 4 },
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

const sec = (minute: number) => at(minute) / 1000;
/** A stand-in for the theme: the colour a token resolves to is its own name, so a spec sees which token each thing drew with. */
const colourOf = (_host: HTMLElement, cssVar: string) => `colour(${cssVar})`;

async function renderChart(fake: FakeTimelineChart, plan = buildTimelinePlan(sessionDetail())) {
  const view = await render(TimelineChartComponent, {
    inputs: { plan },
    providers: [
      { provide: TIMELINE_CHART_FACTORY, useValue: fake.factory },
      { provide: TIMELINE_COLOURS, useValue: colourOf },
    ],
  });
  await view.fixture.whenStable();
  return view;
}

const optionsOf = (fake: FakeTimelineChart) => fake.created[0].options as {
  autoSize: boolean;
  layout: { textColor: string; background: { color: string }; panes: { separatorColor: string } };
  grid: { vertLines: { color: string }; horzLines: { color: string } };
  crosshair: { horzLine: { labelVisible: boolean } };
  localization: { timeFormatter: (time: number) => string };
  handleScroll: boolean;
  handleScale: boolean;
  timeScale: { tickMarkFormatter: (time: number, mark: number) => string; timeVisible: boolean; secondsVisible: boolean; fixLeftEdge?: boolean; fixRightEdge?: boolean };
};

beforeEach(() => markers.create.mockReset());

describe('the one-day chart, making it', () => {
  it('makes the chart once the view has rendered, in an element of its own that has a name', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    expect(fake.created.length).toBe(1);
    const element = screen.getByRole('img', { name: /^Minute closes of SPY\. Contracts drawn: 2\./ });
    expect(fake.created[0].container).toBe(element);
    expect(optionsOf(fake).autoSize).toBe(true);
  });

  it('makes the chart tall enough for its panes: a pane and the axis, and no more', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    expect(screen.getByRole('img').style.height).toBe('604px');
  });

  it('reads every colour of the chart’s surface from the theme’s tokens, and none from a literal', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);
    const options = optionsOf(fake);

    expect(options.layout.textColor).toBe('colour(--text-secondary)');
    expect(options.layout.background.color).toBe('transparent');
    expect([options.grid.vertLines.color, options.grid.horzLines.color, options.layout.panes.separatorColor]).toEqual(Array(3).fill('colour(--border-light)'));
  });

  it('switches the crosshair’s own price label off: a price a person reads is the server’s text', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    expect(optionsOf(fake).crosshair.horzLine.labelVisible).toBe(false);
  });
});

describe('the one-day chart, time', () => {
  it('writes the axis in New York time, whatever the zone of the viewer', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);
    const { tickMarkFormatter } = optionsOf(fake).timeScale;
    // The library’s tick kinds: Time is 3, TimeWithSeconds is 4, DayOfMonth is 2.

    expect(tickMarkFormatter(sec(2), 3)).toBe('09:32');
    expect(tickMarkFormatter(sec(2), 4)).toBe('09:32:00');
    expect(tickMarkFormatter(sec(2), 2)).toBe('Sep 15');
    expect(tickMarkFormatter(sec(390), 3)).toBe('16:00');
  });

  it('writes the crosshair in New York time with the zone marked', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    expect(optionsOf(fake).localization.timeFormatter(sec(2))).toBe('Sep 15, 09:32:00 ET');
  });

  // The call was bought in the bar that closed at 09:38: it filled in that minute, so the tooltip must not head it as an instant to the second.
  it('names the minute under the cursor in the tooltip by the time it ended, in New York time with the zone marked, and not as an instant', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    fake.crosshair({ time: sec(8), point: { x: 40, y: 20 } });

    expect(await screen.findByText('Minute ending 09:38 ET')).toBeTruthy();
    expect(document.querySelector('.tip strong')?.textContent).toBe('Minute ending 09:38 ET');
    expect([...document.querySelectorAll('.tip li')].map((item) => item.textContent)).toContain("Bought SPY Oct 16 '26 585 Call (contracts: 1): filled $2.57, seen $2.55");
  });
});

describe('the one-day chart, what it hands to the library', () => {
  it('adds one line series for each series of the plan, in the plan’s order, on the plan’s pane, in the plan’s style', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    expect(fake.series.map((each) => [each.type, each.pane, each.options['lineStyle'], each.options['lineVisible']])).toEqual([
      ['LineSeries', 0, 0, false], // the clock: a slot for each minute, no line
      ['LineSeries', 0, 0, true], ['LineSeries', 0, 0, true], // SPY: two lines, the minute it did not print between them
      ['LineSeries', 1, 0, true], ['LineSeries', 1, 0, true], // the call: two lines
      ['LineSeries', 2, 0, true], ['LineSeries', 2, 0, true], // the put: a line and a single minute
      ['LineSeries', 1, 0, false], ['LineSeries', 2, 0, false], // the hosts of the fills and the mark
      ['LineSeries', 1, 1, true], ['LineSeries', 1, 2, true], ['LineSeries', 1, 3, true], // starting price dotted, momentum dashed, stop long dashes
    ]);
  });

  it('sets on each series exactly the points of the plan: positions on the axis, and a minute with nothing in it only in the clock', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    const dataOf = (index: number) => fake.series[index].setData.mock.calls.at(-1)?.[0];
    expect(dataOf(0)).toStrictEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map((minute) => ({ time: sec(minute) })));
    expect(dataOf(1)).toStrictEqual([[1, 585.1], [2, 585.2], [3, 585.15], [4, 585.3], [5, 585.25], [6, 585.4]].map(([minute, value]) => ({ time: sec(minute), value })));
    expect(dataOf(2)).toStrictEqual([[8, 585.55], [9, 585.5], [10, 585.45], [11, 585.6], [12, 585.65]].map(([minute, value]) => ({ time: sec(minute), value })));
    expect(dataOf(6)).toStrictEqual([{ time: sec(10), value: 1.25 }]);
    // The three levels, each between its two instants.
    expect(dataOf(9)).toStrictEqual([{ time: sec(2), value: 2 }, { time: sec(7), value: 2 }]);
    expect(dataOf(10)).toStrictEqual([{ time: sec(2), value: 2.5 }, { time: sec(7), value: 2.5 }]);
    expect(dataOf(11)).toStrictEqual([{ time: sec(8), value: 1.93 }, { time: sec(10), value: 1.93 }]);
  });

  it('never hands the library a series that spans a minute with no point', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    for (const index of [1, 2, 3, 4, 5, 6]) {
      const times = (fake.series[index].setData.mock.calls.at(-1)?.[0] as { time: number }[]).map((datum) => datum.time);
      times.slice(1).forEach((time, at_) => expect(time - times[at_]).toBe(60));
    }
  });

  it('hands over colours that are the theme’s chart tokens, one for what each thing is, and no literal colour', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    const colours = fake.series.map((each) => each.options['color']);
    expect(colours).toEqual([
      'colour(--chart-series-blue)',
      'colour(--chart-series-blue)', 'colour(--chart-series-blue)',
      'colour(--chart-series-sky)', 'colour(--chart-series-sky)',
      'colour(--chart-series-sky)', 'colour(--chart-series-sky)',
      'colour(--chart-series-purple)', 'colour(--chart-series-purple)',
      'colour(--chart-series-amber)', 'colour(--chart-series-violet)', 'colour(--chart-series-red)',
    ]);
    expect(JSON.stringify(fake.series.map((each) => each.options))).not.toMatch(/#[0-9a-f]{3,8}/i);
  });

  it('draws no price label, last-value label or crosshair dot of the library’s: a price is read from the server’s text', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    for (const each of fake.series) {
      expect(each.options).toMatchObject({ lastValueVisible: false, priceLineVisible: false, crosshairMarkerVisible: false });
    }
  });

  it('marks every point of a line with a dot only where the line is a single point, so a minute that traded alone is seen', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    // The seventh series is the put’s lone minute (the tenth); no other line of the toy day is one point.
    expect(fake.series.map((each) => each.options['pointMarkersVisible'])).toEqual([false, false, false, false, false, false, true, false, false, false, false, false]);
    expect(fake.series[6].options['pointMarkersRadius']).toBe(3);
  });

  it('hands the markers to the library on the series that hosts them, at the price filled, in words, in the colour and shape of what each is', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    expect(markers.create).toHaveBeenCalledTimes(2);
    const [callHost, callMarkers] = markers.create.mock.calls[0];
    const [putHost, putMarkers] = markers.create.mock.calls[1];
    expect([callHost, putHost]).toEqual([fake.series[7], fake.series[8]]);
    expect(callMarkers).toEqual([
      { id: 'fill:SPY261016C00585000:call:0:entry', time: sec(8), position: 'atPriceMiddle', price: 2.57, shape: 'arrowUp', color: 'colour(--chart-series-green)', text: 'Bought' },
      { id: 'fill:SPY261016C00585000:call:0:exit', time: sec(11), position: 'atPriceMiddle', price: 1.82, shape: 'arrowDown', color: 'colour(--chart-series-orange)', text: 'Sold' },
    ]);
    expect(putMarkers).toEqual([
      { id: 'fill:SPY261016P00585000:put:0:entry', time: sec(4), position: 'atPriceMiddle', price: 1.42, shape: 'arrowUp', color: 'colour(--chart-series-green)', text: 'Bought' },
      { id: 'mark:SPY261016P00585000:put:0', time: sec(10), position: 'atPriceMiddle', price: 1.25, shape: 'square', color: 'colour(--chart-series-purple)', text: 'Marked, not sold' },
    ]);
  });

  it('makes a chart of the panes it has: a day with only the underlying has one', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake, buildTimelinePlan(sessionDetail({ contracts: [], levels: [], fills: [], marks: [] })));

    expect(screen.getByRole('img').style.height).toBe('224px');
    expect(markers.create).not.toHaveBeenCalled();
    expect(new Set(fake.series.map((each) => each.pane))).toEqual(new Set([0]));
  });

  it('draws a contract’s two lines and none of a contract that was withheld', async () => {
    const fake = fakeTimelineChart(vi);
    const plan = buildTimelinePlan(sessionDetail({ contracts: [shownSeries(CALL_585.symbol, CALL_585, [[2, '2.00'], [3, '2.10']]), shownSeries(PUT_585.symbol, PUT_585, [[3, '1.40']])], levels: [], fills: [], marks: [] }));
    await renderChart(fake, plan);

    expect(fake.series.filter((each) => each.pane === 1).length).toBe(1);
    expect(fake.series.filter((each) => each.pane === 2).length).toBe(1);
  });
});

/** A session of 390 minutes, with the first minute's fill and the last minute's mark where a label is cut off if the day touches the edge. */
const FULL_DAY = 390;
function fullDayWithMarkersAtBothEnds() {
  return buildTimelinePlan(fullDay({
    fills: [{ kind: 'entry', side: 'buy', leg_id: 'put', entry_number: 0, contract: PUT_585.symbol, contracts: 1, bar_close_ms: at(1), seen_usd: '1.40', filled_usd: '1.42', exit_reason: null }],
    marks: [{ leg_id: 'put', entry_number: 0, contract: PUT_585.symbol, bar_close_ms: at(FULL_DAY), price_usd: '1.25', owed: false }],
  }));
}

function fullDay(changes: Partial<SessionDetail>): SessionDetail {
  return sessionDetail({
    session_close_ms: at(FULL_DAY),
    underlying: shownSeries('SPY', null, Array.from({ length: FULL_DAY }, (_, minute) => [minute + 1, '585.00'] as const)),
    contracts: [shownSeries(PUT_585.symbol, PUT_585, [[1, '1.40'], [FULL_DAY, '1.25']])],
    levels: [],
    ...changes,
  });
}

describe('the one-day chart, room for a label at the two ends of the day', () => {
  it('has a fill in the first minute and a mark in the last, which is where the day is drawn', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake, fullDayWithMarkersAtBothEnds());

    const [, putMarkers] = markers.create.mock.calls[0];
    expect(putMarkers.map((marker: { time: number; text: string }) => [marker.time, marker.text])).toEqual([[sec(1), 'Bought'], [sec(FULL_DAY), 'Marked, not sold']]);
  });

  it('does not fix the first and last minute to the edges, which would cut the label off', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake, fullDayWithMarkersAtBothEnds());

    const { timeScale } = optionsOf(fake);
    expect(timeScale.fixLeftEdge).not.toBe(true);
    expect(timeScale.fixRightEdge).not.toBe(true);
  });

  it('shows the 390 minutes with 46 places more on each side at 600 px, which is the 57 px that "Marked, not sold" reaches', async () => {
    const fake = fakeTimelineChart(vi, 600);
    await renderChart(fake, fullDayWithMarkersAtBothEnds());

    expect(fake.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: -46, to: 389 + 46 });
    expect(fake.fitContent).not.toHaveBeenCalled();
  });

  it('works the room out again when the plot changes width, since a place is as wide as the plot allows', async () => {
    const fake = fakeTimelineChart(vi, 600);
    await renderChart(fake, fullDayWithMarkersAtBothEnds());

    fake.resize(300);
    expect(fake.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: -120, to: 389 + 120 });
    fake.resize(1200);
    expect(fake.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: -21, to: 389 + 21 });
  });

  it('fits the day as it is while the plot has no width, and gives it its room when the width arrives', async () => {
    const fake = fakeTimelineChart(vi, 0);
    await renderChart(fake, fullDayWithMarkersAtBothEnds());
    expect(fake.fitContent).toHaveBeenCalled();
    expect(fake.setVisibleLogicalRange).not.toHaveBeenCalled();

    fake.resize(600);

    expect(fake.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: -46, to: 389 + 46 });
  });

  it('gives a day with no marker the room that the axis’s own label at an end of it needs, and no more', async () => {
    const fake = fakeTimelineChart(vi, 600);
    await renderChart(fake, buildTimelinePlan(fullDay({ fills: [], marks: [] })));

    // "16:00" reaches 19 px: 14 places on each side of the 390 minutes are 20 px wide at 600 px, and 13 are not.
    expect(fake.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: -14, to: 389 + 14 });
    expect(fake.fitContent).not.toHaveBeenCalled();
  });

  it('works the room out for the day it is then given, when the plan changes', async () => {
    const fake = fakeTimelineChart(vi, 600);
    const view = await renderChart(fake, fullDayWithMarkersAtBothEnds());

    await view.rerender({ inputs: { plan: buildTimelinePlan(sessionDetail()) } });
    await view.fixture.whenStable();

    // The toy day: 12 minutes, and "Marked, not sold" still reaches 57 px.
    expect(fake.setVisibleLogicalRange).toHaveBeenLastCalledWith({ from: -2, to: 11 + 2 });
  });

  it('cannot be scrolled or zoomed, so that the day is never moved out of view', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    const options = optionsOf(fake);
    expect([options.handleScroll, options.handleScale]).toEqual([false, false]);
  });

  it('stops listening for the width when it is taken down', async () => {
    const fake = fakeTimelineChart(vi);
    const view = await renderChart(fake);
    const handler = fake.chart.timeScale().subscribeSizeChange.mock.calls[0][0];

    view.fixture.destroy();

    expect(fake.chart.timeScale().unsubscribeSizeChange).toHaveBeenCalledWith(handler);
  });
});

describe('the one-day chart, the tooltip', () => {
  it('shows, for the minute under the cursor, the server’s own text for the closes, the fill and the level in force', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    fake.crosshair({ time: sec(8), point: { x: 40, y: 20 } });

    await screen.findByText('Minute ending 09:38 ET');
    const lines = [...document.querySelectorAll('.tip li')].map((item) => item.textContent);
    expect(lines).toEqual([
      'SPY: $585.55',
      "SPY Oct 16 '26 585 Call: $2.60",
      "SPY Oct 16 '26 585 Put: no trade this minute",
      "Bought SPY Oct 16 '26 585 Call (contracts: 1): filled $2.57, seen $2.55",
      'Stop level $1.93 (call, first entry)',
    ]);
  });

  it('sits beside the cursor, and on its other side in the right half of the chart so that it is not cut off', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);
    Object.defineProperty(screen.getByRole('img'), 'clientWidth', { value: 1000 });

    fake.crosshair({ time: sec(8), point: { x: 300, y: 20 } });
    await screen.findByText('Minute ending 09:38 ET');
    const tip = () => document.querySelector<HTMLElement>('.tip');
    expect([tip()?.style.left, tip()?.style.top, tip()?.classList.contains('tip--left')]).toEqual(['300px', '20px', false]);

    fake.crosshair({ time: sec(8), point: { x: 800, y: 60 } });
    await waitFor(() => expect(tip()?.classList.contains('tip--left')).toBe(true));
    expect([tip()?.style.left, tip()?.style.top]).toEqual(['800px', '60px']);
  });

  it('is hidden from a screen reader: the lists beside the chart are its equivalent', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);

    fake.crosshair({ time: sec(8), point: { x: 40, y: 20 } });

    await screen.findByText('Minute ending 09:38 ET');
    expect(document.querySelector('.tip')?.getAttribute('aria-hidden')).toBe('true');
  });

  it('goes when the cursor leaves the chart, or is over a minute the plan knows nothing of', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake);
    fake.crosshair({ time: sec(8), point: { x: 40, y: 20 } });
    await screen.findByText('Minute ending 09:38 ET');

    fake.crosshair({});
    await waitFor(() => expect(document.querySelector('.tip')).toBeNull());
    fake.crosshair({ time: sec(8), point: { x: 40, y: 20 } });
    await screen.findByText('Minute ending 09:38 ET');
    fake.crosshair({ time: sec(500), point: { x: 40, y: 20 } });

    await waitFor(() => expect(document.querySelector('.tip')).toBeNull());
  });

  it('shows the text of a price exactly, though a number would have lost digits', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake, buildTimelinePlan(sessionDetail({ underlying: shownSeries('SPY', null, [[1, '9007199254740993.01']]) })));

    fake.crosshair({ time: sec(1), point: { x: 40, y: 20 } });

    await screen.findByText('Minute ending 09:31 ET');
    expect(document.querySelector('.tip li')?.textContent).toBe('SPY: $9,007,199,254,740,993.01');
  });
});

describe('the one-day chart, redrawing and taking down', () => {
  it('takes down what it drew and draws the new plan when the plan changes, on the same chart', async () => {
    const fake = fakeTimelineChart(vi);
    const view = await renderChart(fake);
    const first = [...fake.series];

    await view.rerender({ inputs: { plan: buildTimelinePlan(sessionDetail({ contracts: [], levels: [], fills: [], marks: [] })) } });
    await view.fixture.whenStable();

    expect(fake.created.length).toBe(1);
    expect(fake.chart.removeSeries.mock.calls.map(([each]) => each)).toEqual(first);
    expect(fake.series.length).toBe(first.length + 3);
  });

  it('takes the chart down with the view: it stops listening and removes it, once', async () => {
    const fake = fakeTimelineChart(vi);
    const view = await renderChart(fake);
    const handler = fake.chart.subscribeCrosshairMove.mock.calls[0][0];

    view.fixture.destroy();

    expect(fake.chart.unsubscribeCrosshairMove).toHaveBeenCalledWith(handler);
    expect(fake.chart.remove).toHaveBeenCalledTimes(1);
  });

  it('draws nothing and makes no second chart for a day that was skipped', async () => {
    const fake = fakeTimelineChart(vi);
    await renderChart(fake, buildTimelinePlan(skippedSession()));

    expect(fake.series).toEqual([]);
    expect(screen.getByRole('img', { name: 'Nothing could be drawn for this day.' })).toBeTruthy();
  });
});
