import { describe, expect, it } from 'vitest';

import { CHART_SERIES_ELIGIBLE_TOKENS } from '../../../shared/trading-chart/chart-series-color-tokens';
import type { PlanPoint, PlanSlot, SeriesPlan } from './timeline-plan';
import { buildTimelinePlan, NO_MINUTES, NO_REASON_GIVEN, UNPLACEABLE } from './timeline-plan';
import {
  at, CALL_585, CALL_CLOSES, PUT_585, SCENE_LEVELS, SERIES_WORDS, sessionDetail, shownSeries, skippedSession, withheldSeries,
} from './testing/detail-fixtures';

/** Chart seconds of the close of the bar `minute` minutes after the toy session's open. */
const sec = (minute: number) => at(minute) / 1000;
const isPoint = (datum: PlanPoint | PlanSlot): datum is PlanPoint => 'value' in datum;
const timesOf = (plan: SeriesPlan) => plan.data.map((datum) => datum.time);
const valuesOf = (plan: SeriesPlan) => plan.data.filter(isPoint).map((datum) => datum.value);
const PUT_CLOSES_SHORT: readonly (readonly [number, string])[] = [[3, '1.40'], [4, '1.44']];
const closesOn = (plan: ReturnType<typeof buildTimelinePlan>, pane: number) => plan.series.filter((each) => each.role === 'closes' && each.pane === pane);

describe('the plan of a traded day: the lines of minute closes', () => {
  const plan = buildTimelinePlan(sessionDetail());

  it('has a pane for the underlying and one for each contract, top to bottom, in the order the server sent', () => {
    expect(plan.panes.map((pane) => pane.caption)).toEqual(['SPY', "SPY Oct 16 '26 585 Call", "SPY Oct 16 '26 585 Put"]);
  });

  it('names each pane’s instrument by the parts the server sent, for the shared renderers, and reads no symbol', () => {
    expect(plan.panes.map((pane) => [pane.symbol, pane.contract])).toEqual([['SPY', null], [CALL_585.symbol, CALL_585], [PUT_585.symbol, PUT_585]]);
  });

  it('breaks the underlying’s line in the minute it did not print: two lines, none across the seventh minute', () => {
    const spy = closesOn(plan, 0);

    expect(spy.map(timesOf)).toEqual([
      [1, 2, 3, 4, 5, 6].map(sec),
      [8, 9, 10, 11, 12].map(sec),
    ]);
    expect(spy.map(valuesOf)).toEqual([
      [585.1, 585.2, 585.15, 585.3, 585.25, 585.4],
      [585.55, 585.5, 585.45, 585.6, 585.65],
    ]);
    expect(spy.flatMap(timesOf)).not.toContain(sec(7));
  });

  it('breaks the call’s line over its two minutes with no trade, and starts the second line where it traded again', () => {
    const call = closesOn(plan, 1);

    expect(call.map(timesOf)).toEqual([[2, 3, 4].map(sec), [7, 8, 9, 10, 11, 12].map(sec)]);
    expect(call.flatMap(timesOf)).not.toContain(sec(5));
    expect(call.flatMap(timesOf)).not.toContain(sec(6));
    expect(call.flatMap(timesOf)).not.toContain(sec(1));
  });

  it('draws a minute that traded alone as a line of its own, one point', () => {
    const put = closesOn(plan, 2);

    expect(put.map(timesOf)).toEqual([[3, 4, 5, 6].map(sec), [sec(10)]]);
    expect(valuesOf(put[1])).toEqual([1.25]);
  });

  it('never lets a line span a minute that has no point: in every line one point follows the one before by a single minute', () => {
    for (const line of plan.series.filter((each) => each.role === 'closes')) {
      const times = timesOf(line);
      times.slice(1).forEach((time, at_) => expect(time - times[at_]).toBe(60));
    }
  });

  it('draws exactly the points the server sent, none more, none filled in, carried forward or smoothed', () => {
    const drawn = (pane: number) => closesOn(plan, pane).flatMap(timesOf);

    expect(drawn(0)).toEqual([1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12].map(sec));
    expect(drawn(1)).toEqual([2, 3, 4, 7, 8, 9, 10, 11, 12].map(sec));
    expect(drawn(2)).toEqual([3, 4, 5, 6, 10].map(sec));
    expect(plan.series.filter((each) => each.role === 'closes' && each.data.some((datum) => !isPoint(datum)))).toEqual([]);
  });

  it('draws the lines with a solid style and in the colour of its own token', () => {
    const lines = plan.series.filter((each) => each.role === 'closes');

    expect(lines.every((line) => line.lineStyle === 'solid' && line.lineVisible)).toBe(true);
    expect([...new Set(closesOn(plan, 0).map((line) => line.token))]).toEqual(['series-blue']);
    expect([...new Set([...closesOn(plan, 1), ...closesOn(plan, 2)].map((line) => line.token))]).toEqual(['series-sky']);
  });
});

describe('the plan of a traded day: the clock', () => {
  const plan = buildTimelinePlan(sessionDetail());
  const clock = plan.series.find((each) => each.role === 'clock');

  it('holds a slot for every minute of the session and nothing in any of them, and comes first', () => {
    expect(plan.series[0]).toBe(clock);
    expect(clock?.pane).toBe(0);
    expect(clock?.data).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map((minute) => ({ time: sec(minute) })));
    expect(clock?.lineVisible).toBe(false);
  });

  it('is made for a day of minutes and not for a session that is longer than a day', () => {
    const absurd = buildTimelinePlan(sessionDetail({ session_close_ms: at(0) + 3 * 24 * 60 * 60_000 }));

    expect(absurd.series.find((each) => each.role === 'clock')).toBeUndefined();
    expect(absurd.series.some((each) => each.role === 'closes')).toBe(true);
  });
});

describe('the plan of a traded day: the levels', () => {
  const plan = buildTimelinePlan(sessionDetail());
  const levels = plan.series.filter((each) => each.role === 'level');

  it('draws each level on the pane of its contract, between the two instants the server gave for it and nowhere else', () => {
    expect(levels.map((level) => [level.pane, timesOf(level), valuesOf(level)])).toEqual([
      [1, [sec(2), sec(7)], [2, 2]],
      [1, [sec(2), sec(7)], [2.5, 2.5]],
      [1, [sec(8), sec(10)], [1.93, 1.93]],
    ]);
  });

  it('tells a starting price, a momentum level and a stop apart by how the line is drawn, not by colour alone', () => {
    expect(levels.map((level) => level.lineStyle)).toEqual(['dotted', 'dashed', 'large-dashed']);
    expect(new Set(levels.map((level) => level.token)).size).toBe(3);
  });

  it('draws a level that begins and ends in one minute as one point', () => {
    const one = buildTimelinePlan(sessionDetail({ levels: [{ ...SCENE_LEVELS[2], from_ms: at(9), to_ms: at(9) }] }));

    expect(one.series.filter((each) => each.role === 'level').map(timesOf)).toEqual([[sec(9)]]);
  });

  it('draws a level through the minutes its contract did not trade in, because it is a level and not a price', () => {
    const across = buildTimelinePlan(sessionDetail({ levels: [{ ...SCENE_LEVELS[0], from_ms: at(3), to_ms: at(8) }] }));

    expect(across.series.filter((each) => each.role === 'level').map(timesOf)).toEqual([[sec(3), sec(8)]]);
  });

  it('does not draw a level whose end is before its start, and says so', () => {
    const backwards = buildTimelinePlan(sessionDetail({ levels: [{ ...SCENE_LEVELS[2], from_ms: at(10), to_ms: at(8) }] }));

    expect(backwards.series.filter((each) => each.role === 'level')).toEqual([]);
    expect(backwards.notDrawn.map((each) => [each.what, each.symbol, each.contract, each.message, each.reason])).toEqual([['Stop level', CALL_585.symbol, CALL_585, UNPLACEABLE, null]]);
  });

  it('does not draw a level whose price is not a number, and says so', () => {
    const odd = buildTimelinePlan(sessionDetail({ levels: [{ ...SCENE_LEVELS[2], price_usd: '1e5' }] }));

    expect(odd.series.filter((each) => each.role === 'level')).toEqual([]);
    expect(odd.notDrawn.map((each) => each.message)).toEqual([UNPLACEABLE]);
  });
});

describe('the plan of a traded day: the fills and the marks', () => {
  const plan = buildTimelinePlan(sessionDetail());
  const hosts = plan.series.filter((each) => each.role === 'fills');

  it('puts the call’s two fills on its pane at the price filled, and the put’s purchase and mark on its pane', () => {
    expect(hosts.map((host) => host.pane)).toEqual([1, 2]);
    expect(hosts[0].markers).toEqual([
      { id: 'fill:SPY261016C00585000:call:0:entry', time: sec(8), price: 2.57, shape: 'arrowUp', token: 'series-green', text: 'Bought' },
      { id: 'fill:SPY261016C00585000:call:0:exit', time: sec(11), price: 1.82, shape: 'arrowDown', token: 'series-orange', text: 'Sold' },
    ]);
    expect(hosts[1].markers).toEqual([
      { id: 'fill:SPY261016P00585000:put:0:entry', time: sec(4), price: 1.42, shape: 'arrowUp', token: 'series-green', text: 'Bought' },
      { id: 'mark:SPY261016P00585000:put:0', time: sec(10), price: 1.25, shape: 'square', token: 'series-purple', text: 'Marked, not sold' },
    ]);
  });

  it('gives the host series a point at each marker’s instant and no line, so a marker is never placed on a minute with nothing', () => {
    expect(hosts.map((host) => [timesOf(host), host.lineVisible])).toEqual([[[sec(8), sec(11)], false], [[sec(4), sec(10)], false]]);
  });

  it('tells a sale and a mark apart: the mark is a square of its own, not an arrow', () => {
    const shapes = hosts.flatMap((host) => host.markers).map((marker) => marker.shape);

    expect(shapes).toEqual(['arrowUp', 'arrowDown', 'arrowUp', 'square']);
  });

  it('keeps two fills in one minute on one point, each marker at its own price', () => {
    const twice = buildTimelinePlan(
      sessionDetail({
        fills: [
          { kind: 'entry', leg_id: 'call', entry_number: 0, contract: CALL_585.symbol, contracts: 1, bar_close_ms: at(8), seen_usd: '2.55', filled_usd: '2.57', exit_reason: null },
          { kind: 'entry', leg_id: 'call_2', entry_number: 0, contract: CALL_585.symbol, contracts: 1, bar_close_ms: at(8), seen_usd: '2.55', filled_usd: '2.58', exit_reason: null },
        ],
        marks: [],
      }),
    );

    const host = twice.series.find((each) => each.role === 'fills');
    expect(host?.data).toEqual([{ time: sec(8), value: 2.57 }]);
    expect(host?.markers.map((marker) => marker.price)).toEqual([2.57, 2.58]);
  });

  it('does not place a marker whose price is not a number, and says so', () => {
    const odd = buildTimelinePlan(sessionDetail({ marks: [{ ...sessionDetail().marks[0], price_usd: 'NaN' }] }));

    expect(odd.series.filter((each) => each.role === 'fills').flatMap((host) => host.markers).map((marker) => marker.shape)).toEqual(['arrowUp', 'arrowDown', 'arrowUp']);
    expect(odd.notDrawn.map((each) => [each.what, each.symbol, each.message])).toEqual([['Marked, not sold', PUT_585.symbol, UNPLACEABLE]]);
  });
});

describe('the plan of a traded day: the legend, pane by pane', () => {
  const plan = buildTimelinePlan(sessionDetail());

  it('names in words each line and mark of a pane and how it is drawn', () => {
    expect(plan.panes.map((pane) => pane.entries.map((entry) => `${entry.name} (${entry.drawn})`))).toEqual([
      ['Minute closes (solid line)'],
      ['Minute closes (solid line)', 'Bought (up arrow)', 'Sold (down arrow)', 'Starting price (dotted line)', 'Momentum level (dashed line)', 'Stop level (long dashes)'],
      ['Minute closes (solid line)', 'A lone minute with a trade (dot)', 'Bought (up arrow)', 'Marked, not sold (square)'],
    ]);
  });

  it('gives each legend entry a token id and a way of being drawn, and every token is one the chart palette has', () => {
    const entries = plan.panes.flatMap((pane) => pane.entries);

    expect(entries.every((entry) => CHART_SERIES_ELIGIBLE_TOKENS.includes(entry.token))).toBe(true);
    expect(plan.series.every((each) => CHART_SERIES_ELIGIBLE_TOKENS.includes(each.token))).toBe(true);
    expect(entries.filter((entry) => entry.sample === 'marker').every((entry) => entry.glyph !== '')).toBe(true);
  });

  it('is a name for the chart that says what is drawn in it and that the same facts are listed as text', () => {
    expect(plan.label).toBe('Minute closes of SPY. Contracts drawn: 2. Fills drawn: 3. Marks drawn: 1. Levels drawn: 3. The same facts are listed below as text.');
  });
});

describe('the plan of a traded day: a minute that traded alone', () => {
  const plan = buildTimelinePlan(sessionDetail());

  it('gives a dot to the line of one point and to no line of more, and to nothing that is not a line of minute closes', () => {
    expect(plan.series.filter((each) => each.pointMarkers).map((each) => [each.role, each.pane, timesOf(each)])).toEqual([['closes', 2, [sec(10)]]]);
  });

  it('gives no dot to a line of two points, which the library draws as a line', () => {
    const two = buildTimelinePlan(sessionDetail({ contracts: [shownSeries(CALL_585.symbol, CALL_585, [[2, '2.00'], [3, '2.10'], [6, '2.20']])], levels: [], fills: [], marks: [] }));

    expect(two.series.filter((each) => each.role === 'closes' && each.pane === 1).map((each) => [timesOf(each), each.pointMarkers])).toEqual([[[sec(2), sec(3)], false], [[sec(6)], true]]);
  });

  it('says the dot in the legend of the pane that holds one, and of no other', () => {
    expect(plan.panes.map((pane) => pane.entries.some((entry) => entry.drawn === 'dot'))).toEqual([false, false, true]);
    expect(plan.panes[2].entries.find((entry) => entry.drawn === 'dot')).toMatchObject({ name: 'A lone minute with a trade', sample: 'marker', glyph: '●', token: 'series-sky' });
  });
});

describe('the name of the chart', () => {
  it('counts what is drawn and, apart, how much of the day is not, when a contract’s series is withheld', () => {
    const plan = buildTimelinePlan(sessionDetail({ contracts: [withheldSeries(CALL_585.symbol, CALL_585, 'changed'), shownSeries(PUT_585.symbol, PUT_585, PUT_CLOSES_SHORT)] }));

    // The put is drawn with its purchase and its mark; the call’s series, its two fills and its three levels are not.
    expect(plan.label).toBe('Minute closes of SPY. Contracts drawn: 1. Fills drawn: 1. Marks drawn: 1. Levels drawn: 0. Not drawn: series 1, fills 2, marks 0, levels 3. The same facts are listed below as text.');
  });

  it('says that the underlying is not drawn when it is withheld, and counts it among the series not drawn', () => {
    const plan = buildTimelinePlan(sessionDetail({ underlying: withheldSeries('SPY', null, 'lake_unavailable') }));

    expect(plan.label).toBe('Minute closes; the underlying SPY is not drawn. Contracts drawn: 2. Fills drawn: 3. Marks drawn: 1. Levels drawn: 3. Not drawn: series 1, fills 0, marks 0, levels 0. The same facts are listed below as text.');
  });

  it('counts a level or a mark the page could not place among those not drawn, not among those drawn', () => {
    const plan = buildTimelinePlan(sessionDetail({ levels: [SCENE_LEVELS[0], { ...SCENE_LEVELS[2], price_usd: '1e5' }], marks: [{ ...sessionDetail().marks[0], price_usd: 'NaN' }] }));

    expect(plan.label).toBe('Minute closes of SPY. Contracts drawn: 2. Fills drawn: 3. Marks drawn: 0. Levels drawn: 1. Not drawn: series 0, fills 0, marks 1, levels 1. The same facts are listed below as text.');
  });

  it('names a single contract with a number and a label, and needs no plural for it', () => {
    const plan = buildTimelinePlan(sessionDetail({ contracts: [shownSeries(CALL_585.symbol, CALL_585, CALL_CLOSES)], levels: [], fills: [], marks: [] }));

    expect(plan.label).toBe('Minute closes of SPY. Contracts drawn: 1. Fills drawn: 0. Marks drawn: 0. Levels drawn: 0. The same facts are listed below as text.');
  });
});

describe('the plan of a traded day: what the tooltip says, in the server’s own text', () => {
  const plan = buildTimelinePlan(sessionDetail());
  const call = "SPY Oct 16 '26 585 Call";
  const put = "SPY Oct 16 '26 585 Put";

  it('lists, for the minute a contract filled in, its close, the fill with both prices, and the stop level in force', () => {
    expect(plan.tips.get(sec(8))).toEqual([
      'SPY: $585.55',
      `${call}: $2.60`,
      `${put}: no trade this minute`,
      `Bought ${call} (contracts: 1): filled $2.57, seen $2.55`,
      'Stop level $1.93 (call, first entry)',
    ]);
  });

  it('says a series did not trade in a minute, so a break is explained where it is', () => {
    expect(plan.tips.get(sec(7))).toEqual([
      'SPY: no trade this minute',
      `${call}: $2.50`,
      `${put}: no trade this minute`,
      'Starting price $2.00 (call, first entry)',
      'Momentum level $2.50 (call, first entry)',
    ]);
  });

  it('says what a sale was for, and a mark is told apart from a sale', () => {
    expect(plan.tips.get(sec(11))?.filter((line) => line.startsWith('Sold'))).toEqual([`Sold ${call} (contracts: 1): filled $1.82, seen $1.84 (Stop)`]);
    expect(plan.tips.get(sec(10))?.filter((line) => line.startsWith('Marked'))).toEqual([`Marked, not sold: ${put} at $1.25`]);
  });

  it('has lines for every minute of the day and for no minute that is not in it', () => {
    expect([...plan.tips.keys()].sort((left, right) => left - right)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map(sec));
  });

  it('shows a price as the server wrote it, though a number would have lost digits', () => {
    const exact = buildTimelinePlan(sessionDetail({ underlying: shownSeries('SPY', null, [[1, '9007199254740993.01'], [2, '1.2345']]) }));

    expect(exact.tips.get(sec(1))?.[0]).toBe('SPY: $9,007,199,254,740,993.01');
    expect(exact.tips.get(sec(2))?.[0]).toBe('SPY: 1.2345');
  });
});

describe('the plan of a day with a series the server withholds', () => {
  it('draws no pane for a withheld contract, says why in the server’s words, and draws none of its levels, fills or marks', () => {
    const plan = buildTimelinePlan(sessionDetail({ contracts: [withheldSeries(CALL_585.symbol, CALL_585, 'changed'), shownSeries(PUT_585.symbol, PUT_585, [[3, '1.40'], [4, '1.44']])] }));

    expect(plan.panes.map((pane) => pane.caption)).toEqual(['SPY', "SPY Oct 16 '26 585 Put"]);
    expect(plan.notDrawn).toEqual([{ key: CALL_585.symbol, what: null, symbol: CALL_585.symbol, ofUnderlying: false, contract: CALL_585, reason: 'changed', message: SERIES_WORDS.changed, listedBelow: true }]);
    expect(plan.series.filter((each) => each.role === 'level')).toEqual([]);
    expect(plan.series.filter((each) => each.role === 'fills').map((host) => host.markers.map((marker) => marker.id))).toEqual([
      ['fill:SPY261016P00585000:put:0:entry', 'mark:SPY261016P00585000:put:0'],
    ]);
    expect(plan.series.every((each) => each.pane <= 1)).toBe(true);
  });

  it('keeps nothing of a withheld contract in the tooltip', () => {
    const plan = buildTimelinePlan(sessionDetail({ contracts: [withheldSeries(CALL_585.symbol, CALL_585, 'gone'), shownSeries(PUT_585.symbol, PUT_585, PUT_CLOSES_SHORT)] }));

    expect([...plan.tips.values()].flat().filter((line) => line.includes('Call'))).toEqual([]);
  });

  it('draws no mark and puts none in a tooltip for a contract whose series is withheld, though the mark is still a fact the day lists', () => {
    // The put was marked at 1.25 in the tenth minute and not sold. Its series is withheld, so no pane holds a point for the mark to sit on.
    const plan = buildTimelinePlan(sessionDetail({ contracts: [shownSeries(CALL_585.symbol, CALL_585, CALL_CLOSES), withheldSeries(PUT_585.symbol, PUT_585, 'gone')] }));

    const markers = plan.series.flatMap((each) => each.markers);
    expect(plan.panes.map((pane) => pane.caption)).toEqual(['SPY', "SPY Oct 16 '26 585 Call"]);
    expect(markers.filter((marker) => marker.shape === 'square')).toEqual([]);
    expect(markers.map((marker) => marker.id)).toEqual(['fill:SPY261016C00585000:call:0:entry', 'fill:SPY261016C00585000:call:0:exit']);
    expect([...plan.tips.values()].flat().filter((line) => line.startsWith('Marked'))).toEqual([]);
    expect(plan.series.every((each) => each.pane <= 1)).toBe(true);
    expect(plan.notDrawn.map((each) => [each.symbol, each.listedBelow])).toEqual([[PUT_585.symbol, true]]);
  });

  it('says in the page’s own words, beside the reason it has, that the server gave no message for a series it withheld', () => {
    for (const message of [null, '', '   ']) {
      const plan = buildTimelinePlan(sessionDetail({ underlying: { ...withheldSeries('SPY', null, 'gone'), message } }));

      expect(plan.notDrawn.map((each) => [each.reason, each.message])).toEqual([['gone', NO_REASON_GIVEN]]);
    }
  });

  it('says the same of every reason, each in its own words', () => {
    for (const reason of ['session_skipped', 'no_pin', 'gone', 'changed', 'not_confirmed', 'unreadable', 'lake_unavailable'] as const) {
      const plan = buildTimelinePlan(sessionDetail({ underlying: withheldSeries('SPY', null, reason) }));

      expect(plan.notDrawn).toEqual([{ key: 'SPY', what: null, symbol: 'SPY', ofUnderlying: true, contract: null, reason, message: SERIES_WORDS[reason], listedBelow: false }]);
    }
  });

  it('draws the contracts when the underlying is withheld, the first of them on the top pane with the clock', () => {
    const plan = buildTimelinePlan(sessionDetail({ underlying: withheldSeries('SPY', null, 'lake_unavailable') }));

    expect(plan.panes.map((pane) => pane.caption)).toEqual(["SPY Oct 16 '26 585 Call", "SPY Oct 16 '26 585 Put"]);
    expect(plan.series[0]).toMatchObject({ role: 'clock', pane: 0 });
    expect(closesOn(plan, 0).flatMap(timesOf)).toEqual([2, 3, 4, 7, 8, 9, 10, 11, 12].map(sec));
    expect(closesOn(plan, 1).flatMap(timesOf)).toEqual([3, 4, 5, 6, 10].map(sec));
  });

  it('draws nothing at all for a skipped day, and gives the server’s words for the one series it sent', () => {
    const plan = buildTimelinePlan(skippedSession());

    expect(plan.series).toEqual([]);
    expect(plan.panes).toEqual([]);
    expect(plan.tips.size).toBe(0);
    expect(plan.notDrawn).toEqual([{ key: 'SPY', what: null, symbol: 'SPY', ofUnderlying: true, contract: null, reason: 'session_skipped', message: SERIES_WORDS.session_skipped, listedBelow: false }]);
    expect(plan.label).toBe('Nothing could be drawn for this day.');
  });

  it('draws nothing where a shown series holds no minute, and says so', () => {
    const plan = buildTimelinePlan(sessionDetail({ contracts: [shownSeries(CALL_585.symbol, CALL_585, [])] }));

    expect(plan.notDrawn).toEqual([{ key: CALL_585.symbol, what: null, symbol: CALL_585.symbol, ofUnderlying: false, contract: CALL_585, reason: null, message: NO_MINUTES, listedBelow: true }]);
  });

  it('does not draw a series whose minutes are out of order, whose price is not a number, or that repeats a minute, and says so', () => {
    const outOfOrder = shownSeries(CALL_585.symbol, CALL_585, [[3, '2.00'], [2, '2.10']]);
    const notANumber = shownSeries(CALL_585.symbol, CALL_585, [[2, '2.00'], [3, '0x10']]);
    const twice = shownSeries(CALL_585.symbol, CALL_585, [[2, '2.00'], [2, '2.10']]);

    for (const bad of [outOfOrder, notANumber, twice]) {
      const plan = buildTimelinePlan(sessionDetail({ contracts: [bad] }));

      expect(plan.panes.map((pane) => pane.caption)).toEqual(['SPY']);
      expect(plan.notDrawn.map((each) => [each.reason, each.message])).toEqual([[null, UNPLACEABLE]]);
    }
  });
});

