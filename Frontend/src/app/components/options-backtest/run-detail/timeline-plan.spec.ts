import { describe, expect, it } from 'vitest';

import { CHART_SERIES_ELIGIBLE_TOKENS } from '../../../shared/trading-chart/chart-series-color-tokens';
import type { PlanPoint, PlanSlot, SeriesPlan } from './timeline-plan';
import { buildTimelinePlan, NO_MINUTES, NO_REASON_GIVEN, UNPLACEABLE } from './timeline-plan';
import {
  at, CALL_585, CALL_CLOSES, PUT_585, SCENE_LEVELS, SERIES_WORDS, sessionDetail, shownSeries, skippedSession, withheldSeries,
} from './testing/detail-fixtures';
import { owedSession, PUT_580, spreadSession } from './testing/selling-fixtures';

/** Chart seconds of the close of the bar `minute` minutes after the toy session's open. */
const sec = (minute: number) => at(minute) / 1000;
const isPoint = (datum: PlanPoint | PlanSlot): datum is PlanPoint => 'value' in datum;
const timesOf = (plan: SeriesPlan) => plan.data.map((datum) => datum.time);
const valuesOf = (plan: SeriesPlan) => plan.data.filter(isPoint).map((datum) => datum.value);
const PUT_CLOSES_SHORT: readonly (readonly [number, string])[] = [[3, '1.40'], [4, '1.44']];
const closesOn = (plan: ReturnType<typeof buildTimelinePlan>, pane: number) => plan.series.filter((each) => each.role === 'closes' && each.pane === pane);

describe('the plan of a traded day: the lines of minute closes', () => {
  const plan = buildTimelinePlan(sessionDetail());

  it('plans the lines of minute closes: a pane for each instrument, a break where a series did not trade, only the points the server sent, and a solid style in a token of its own', () => {
    expect.soft(plan.panes.map((pane) => pane.caption), 'has a pane for the underlying and one for each contract, top to bottom, in the order the server sent').toEqual(['SPY', "SPY Oct 16 '26 585 Call", "SPY Oct 16 '26 585 Put"]);
    expect.soft(plan.panes.map((pane) => [pane.symbol, pane.contract]), 'names each pane’s instrument by the parts the server sent, for the shared renderers, and reads no symbol').toEqual([['SPY', null], [CALL_585.symbol, CALL_585], [PUT_585.symbol, PUT_585]]);

    const spy = closesOn(plan, 0);
    expect.soft(spy.map(timesOf), 'breaks the underlying’s line in the minute it did not print: two lines, none across the seventh minute (times)').toEqual([
      [1, 2, 3, 4, 5, 6].map(sec),
      [8, 9, 10, 11, 12].map(sec),
    ]);
    expect.soft(spy.map(valuesOf), 'breaks the underlying’s line in the minute it did not print: two lines, none across the seventh minute (values)').toEqual([
      [585.1, 585.2, 585.15, 585.3, 585.25, 585.4],
      [585.55, 585.5, 585.45, 585.6, 585.65],
    ]);
    expect.soft(spy.flatMap(timesOf), 'breaks the underlying’s line in the minute it did not print: no point in the seventh minute').not.toContain(sec(7));

    const call = closesOn(plan, 1);
    expect.soft(call.map(timesOf), 'breaks the call’s line over its two minutes with no trade, and starts the second line where it traded again').toEqual([[2, 3, 4].map(sec), [7, 8, 9, 10, 11, 12].map(sec)]);
    expect.soft(call.flatMap(timesOf), 'breaks the call’s line over its two minutes with no trade: no point in the fifth minute').not.toContain(sec(5));
    expect.soft(call.flatMap(timesOf), 'breaks the call’s line over its two minutes with no trade: no point in the sixth minute').not.toContain(sec(6));
    expect.soft(call.flatMap(timesOf), 'breaks the call’s line over its two minutes with no trade: no point in the first minute').not.toContain(sec(1));

    const put = closesOn(plan, 2);
    expect.soft(put.map(timesOf), 'draws a minute that traded alone as a line of its own').toEqual([[3, 4, 5, 6].map(sec), [sec(10)]]);
    expect.soft(valuesOf(put[1]), 'draws a minute that traded alone as a line of one point').toEqual([1.25]);

    for (const line of plan.series.filter((each) => each.role === 'closes')) {
      const times = timesOf(line);
      times.slice(1).forEach((time, at_) => expect.soft(time - times[at_], 'never lets a line span a minute that has no point: in every line one point follows the one before by a single minute').toBe(60));
    }

    const drawn = (pane: number) => closesOn(plan, pane).flatMap(timesOf);
    expect.soft(drawn(0), 'draws exactly the points the server sent for the underlying, none more, none filled in, carried forward or smoothed').toEqual([1, 2, 3, 4, 5, 6, 8, 9, 10, 11, 12].map(sec));
    expect.soft(drawn(1), 'draws exactly the points the server sent for the call, none more, none filled in, carried forward or smoothed').toEqual([2, 3, 4, 7, 8, 9, 10, 11, 12].map(sec));
    expect.soft(drawn(2), 'draws exactly the points the server sent for the put, none more, none filled in, carried forward or smoothed').toEqual([3, 4, 5, 6, 10].map(sec));
    expect.soft(plan.series.filter((each) => each.role === 'closes' && each.data.some((datum) => !isPoint(datum))), 'draws no slot without a point in a line of minute closes').toEqual([]);

    const lines = plan.series.filter((each) => each.role === 'closes');
    expect.soft(lines.every((line) => line.lineStyle === 'solid' && line.lineVisible), 'draws the lines with a solid style').toBe(true);
    expect.soft([...new Set(closesOn(plan, 0).map((line) => line.token))], 'draws the underlying’s lines in the colour of its own token').toEqual(['series-blue']);
    expect.soft([...new Set([...closesOn(plan, 1), ...closesOn(plan, 2)].map((line) => line.token))], 'draws the contracts’ lines in the colour of their own token').toEqual(['series-sky']);
  });
});

describe('the plan of a traded day: the clock', () => {
  const plan = buildTimelinePlan(sessionDetail());
  const clock = plan.series.find((each) => each.role === 'clock');

  it('holds a slot for every minute of the session and nothing in any of them, comes first, and is made for a day of minutes and not for a session that is longer than a day', () => {
    expect.soft(plan.series[0], 'holds the clock first among the series').toBe(clock);
    expect.soft(clock?.pane, 'holds the clock on the top pane').toBe(0);
    expect.soft(clock?.data, 'holds a slot for every minute of the session and nothing in any of them').toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map((minute) => ({ time: sec(minute) })));
    expect.soft(clock?.lineVisible, 'draws no line for the clock').toBe(false);

    const absurd = buildTimelinePlan(sessionDetail({ session_close_ms: at(0) + 3 * 24 * 60 * 60_000 }));
    expect.soft(absurd.series.find((each) => each.role === 'clock'), 'makes no clock for a session that is longer than a day').toBeUndefined();
    expect.soft(absurd.series.some((each) => each.role === 'closes'), 'still draws the minute closes of a session that is longer than a day').toBe(true);
  });
});

describe('the plan of a traded day: the levels', () => {
  const plan = buildTimelinePlan(sessionDetail());
  const levels = plan.series.filter((each) => each.role === 'level');

  it('plans the levels: each on the pane of its contract between its two instants, told apart by how it is drawn, and not drawn, with a word of it, where it cannot be placed', () => {
    expect.soft(levels.map((level) => [level.pane, timesOf(level), valuesOf(level)]), 'draws each level on the pane of its contract, between the two instants the server gave for it and nowhere else').toEqual([
      [1, [sec(2), sec(7)], [2, 2]],
      [1, [sec(2), sec(7)], [2.5, 2.5]],
      [1, [sec(8), sec(10)], [1.93, 1.93]],
    ]);
    expect.soft(levels.map((level) => level.lineStyle), 'tells a starting price, a momentum level and a stop apart by how the line is drawn').toEqual(['dotted', 'dashed', 'large-dashed']);
    expect.soft(new Set(levels.map((level) => level.token)).size, 'tells a starting price, a momentum level and a stop apart by colour as well: three tokens').toBe(3);

    const one = buildTimelinePlan(sessionDetail({ levels: [{ ...SCENE_LEVELS[2], from_ms: at(9), to_ms: at(9) }] }));
    expect.soft(one.series.filter((each) => each.role === 'level').map(timesOf), 'draws a level that begins and ends in one minute as one point').toEqual([[sec(9)]]);

    const across = buildTimelinePlan(sessionDetail({ levels: [{ ...SCENE_LEVELS[0], from_ms: at(3), to_ms: at(8) }] }));
    expect.soft(across.series.filter((each) => each.role === 'level').map(timesOf), 'draws a level through the minutes its contract did not trade in, because it is a level and not a price').toEqual([[sec(3), sec(8)]]);

    const backwards = buildTimelinePlan(sessionDetail({ levels: [{ ...SCENE_LEVELS[2], from_ms: at(10), to_ms: at(8) }] }));
    expect.soft(backwards.series.filter((each) => each.role === 'level'), 'does not draw a level whose end is before its start').toEqual([]);
    expect.soft(backwards.notDrawn.map((each) => [each.what, each.symbol, each.contract, each.message, each.reason]), 'says so of a level whose end is before its start').toEqual([['Stop level', CALL_585.symbol, CALL_585, UNPLACEABLE, null]]);

    const odd = buildTimelinePlan(sessionDetail({ levels: [{ ...SCENE_LEVELS[2], price_usd: '1e5' }] }));
    expect.soft(odd.series.filter((each) => each.role === 'level'), 'does not draw a level whose price is not a number').toEqual([]);
    expect.soft(odd.notDrawn.map((each) => each.message), 'says so of a level whose price is not a number').toEqual([UNPLACEABLE]);
  });
});

describe('the plan of a traded day: the fills and the marks', () => {
  const plan = buildTimelinePlan(sessionDetail());
  const hosts = plan.series.filter((each) => each.role === 'fills');

  it('plans the fills and the marks: on the pane of their contract at the price filled, on a host series of points with no line, a mark told apart from a sale, and not placed where the price is not a number', () => {
    expect.soft(hosts.map((host) => host.pane), 'puts the call’s fills and the put’s purchase and mark on the pane of their contract').toEqual([1, 2]);
    expect.soft(hosts[0].markers, 'puts the call’s two fills on its pane at the price filled').toEqual([
      { id: 'fill:SPY261016C00585000:call:0:entry', time: sec(8), price: 2.57, shape: 'arrowUp', token: 'series-green', text: 'Bought' },
      { id: 'fill:SPY261016C00585000:call:0:exit', time: sec(11), price: 1.82, shape: 'arrowDown', token: 'series-orange', text: 'Sold' },
    ]);
    expect.soft(hosts[1].markers, 'puts the put’s purchase and mark on its pane').toEqual([
      { id: 'fill:SPY261016P00585000:put:0:entry', time: sec(4), price: 1.42, shape: 'arrowUp', token: 'series-green', text: 'Bought' },
      { id: 'mark:SPY261016P00585000:put:0', time: sec(10), price: 1.25, shape: 'square', token: 'series-purple', text: 'Marked, not sold' },
    ]);
    expect.soft(hosts.map((host) => [timesOf(host), host.lineVisible]), 'gives the host series a point at each marker’s instant and no line, so a marker is never placed on a minute with nothing').toEqual([[[sec(8), sec(11)], false], [[sec(4), sec(10)], false]]);

    const shapes = hosts.flatMap((host) => host.markers).map((marker) => marker.shape);
    expect.soft(shapes, 'tells a sale and a mark apart: the mark is a square of its own, not an arrow').toEqual(['arrowUp', 'arrowDown', 'arrowUp', 'square']);

    const twice = buildTimelinePlan(
      sessionDetail({
        fills: [
          { kind: 'entry', side: 'buy', leg_id: 'call', entry_number: 0, contract: CALL_585.symbol, contracts: 1, bar_close_ms: at(8), seen_usd: '2.55', filled_usd: '2.57', exit_reason: null },
          { kind: 'entry', side: 'buy', leg_id: 'call_2', entry_number: 0, contract: CALL_585.symbol, contracts: 1, bar_close_ms: at(8), seen_usd: '2.55', filled_usd: '2.58', exit_reason: null },
        ],
        marks: [],
      }),
    );

    const twiceHost = twice.series.find((each) => each.role === 'fills');
    expect.soft(twiceHost?.data, 'keeps two fills in one minute on one point').toEqual([{ time: sec(8), value: 2.57 }]);
    expect.soft(twiceHost?.markers.map((marker) => marker.price), 'keeps two fills in one minute each at its own price').toEqual([2.57, 2.58]);

    const odd = buildTimelinePlan(sessionDetail({ marks: [{ ...sessionDetail().marks[0], price_usd: 'NaN' }] }));
    expect.soft(odd.series.filter((each) => each.role === 'fills').flatMap((host) => host.markers).map((marker) => marker.shape), 'does not place a marker whose price is not a number').toEqual(['arrowUp', 'arrowDown', 'arrowUp']);
    expect.soft(odd.notDrawn.map((each) => [each.what, each.symbol, each.message]), 'says so of a marker whose price is not a number').toEqual([['Marked, not sold', PUT_585.symbol, UNPLACEABLE]]);
  });
});

describe('the plan of a traded day: the legend, pane by pane', () => {
  const plan = buildTimelinePlan(sessionDetail());

  it('names in words each line and mark of a pane and how it is drawn, gives each entry a token from the chart palette, and names the chart for what is drawn in it', () => {
    expect.soft(plan.panes.map((pane) => pane.entries.map((entry) => `${entry.name} (${entry.drawn})`)), 'names in words each line and mark of a pane and how it is drawn').toEqual([
      ['Minute closes (solid line)'],
      ['Minute closes (solid line)', 'Bought (up arrow)', 'Sold (down arrow)', 'Starting price (dotted line)', 'Momentum level (dashed line)', 'Stop level (long dashes)'],
      ['Minute closes (solid line)', 'A lone minute with a trade (dot)', 'Bought (up arrow)', 'Marked, not sold (square)'],
    ]);

    const entries = plan.panes.flatMap((pane) => pane.entries);
    expect.soft(entries.every((entry) => CHART_SERIES_ELIGIBLE_TOKENS.includes(entry.token)), 'gives each legend entry a token the chart palette has').toBe(true);
    expect.soft(plan.series.every((each) => CHART_SERIES_ELIGIBLE_TOKENS.includes(each.token)), 'gives each series a token the chart palette has').toBe(true);
    expect.soft(entries.filter((entry) => entry.sample === 'marker').every((entry) => entry.glyph !== ''), 'gives each marker entry a way of being drawn').toBe(true);

    expect.soft(plan.label, 'is a name for the chart that says what is drawn in it and that the same facts are listed as text').toBe('Minute closes of SPY. Contracts drawn: 2. Fills drawn: 3. Marks drawn: 1. Levels drawn: 3. The same facts are listed below as text.');
  });
});

describe('the plan of a traded day: a minute that traded alone', () => {
  const plan = buildTimelinePlan(sessionDetail());

  it('gives a dot to the line of one point and no other, says it in the legend of the pane that holds one, and gives none to a line of two points', () => {
    expect.soft(plan.series.filter((each) => each.pointMarkers).map((each) => [each.role, each.pane, timesOf(each)]), 'gives a dot to the line of one point and to no line of more, and to nothing that is not a line of minute closes').toEqual([['closes', 2, [sec(10)]]]);

    const two = buildTimelinePlan(sessionDetail({ contracts: [shownSeries(CALL_585.symbol, CALL_585, [[2, '2.00'], [3, '2.10'], [6, '2.20']])], levels: [], fills: [], marks: [] }));
    expect.soft(two.series.filter((each) => each.role === 'closes' && each.pane === 1).map((each) => [timesOf(each), each.pointMarkers]), 'gives no dot to a line of two points, which the library draws as a line').toEqual([[[sec(2), sec(3)], false], [[sec(6)], true]]);

    expect.soft(plan.panes.map((pane) => pane.entries.some((entry) => entry.drawn === 'dot')), 'says the dot in the legend of the pane that holds one, and of no other').toEqual([false, false, true]);
    expect.soft(plan.panes[2].entries.find((entry) => entry.drawn === 'dot'), 'says the dot in the legend with its name, glyph and token').toMatchObject({ name: 'A lone minute with a trade', sample: 'marker', glyph: '●', token: 'series-sky' });
  });
});

describe('the name of the chart', () => {
  it.each([
    [
      'counts what is drawn and, apart, how much of the day is not, when a contract’s series is withheld',
      // The put is drawn with its purchase and its mark; the call’s series, its two fills and its three levels are not.
      sessionDetail({ contracts: [withheldSeries(CALL_585.symbol, CALL_585, 'changed'), shownSeries(PUT_585.symbol, PUT_585, PUT_CLOSES_SHORT)] }),
      'Minute closes of SPY. Contracts drawn: 1. Fills drawn: 1. Marks drawn: 1. Levels drawn: 0. Not drawn: series 1, fills 2, marks 0, levels 3. The same facts are listed below as text.',
    ],
    [
      'says that the underlying is not drawn when it is withheld, and counts it among the series not drawn',
      sessionDetail({ underlying: withheldSeries('SPY', null, 'lake_unavailable') }),
      'Minute closes; the underlying SPY is not drawn. Contracts drawn: 2. Fills drawn: 3. Marks drawn: 1. Levels drawn: 3. Not drawn: series 1, fills 0, marks 0, levels 0. The same facts are listed below as text.',
    ],
    [
      'counts a level or a mark the page could not place among those not drawn, not among those drawn',
      sessionDetail({ levels: [SCENE_LEVELS[0], { ...SCENE_LEVELS[2], price_usd: '1e5' }], marks: [{ ...sessionDetail().marks[0], price_usd: 'NaN' }] }),
      'Minute closes of SPY. Contracts drawn: 2. Fills drawn: 3. Marks drawn: 0. Levels drawn: 1. Not drawn: series 0, fills 0, marks 1, levels 1. The same facts are listed below as text.',
    ],
    [
      'names a single contract with a number and a label, and needs no plural for it',
      sessionDetail({ contracts: [shownSeries(CALL_585.symbol, CALL_585, CALL_CLOSES)], levels: [], fills: [], marks: [] }),
      'Minute closes of SPY. Contracts drawn: 1. Fills drawn: 0. Marks drawn: 0. Levels drawn: 0. The same facts are listed below as text.',
    ],
  ])('%s', (_name, day, label) => {
    expect(buildTimelinePlan(day).label).toBe(label);
  });
});

describe('the plan of a traded day: what the tooltip says, in the server’s own text', () => {
  const plan = buildTimelinePlan(sessionDetail());
  const call = "SPY Oct 16 '26 585 Call";
  const put = "SPY Oct 16 '26 585 Put";

  it('lists in the server’s own text, for each minute, the closes, the fills with both prices, the stop level in force and what a sale was for, and shows a price as the server wrote it', () => {
    expect.soft(plan.tips.get(sec(8)), 'lists, for the minute a contract filled in, its close, the fill with both prices, and the stop level in force').toEqual([
      'SPY: $585.55',
      `${call}: $2.60`,
      `${put}: no trade this minute`,
      `Bought ${call} (contracts: 1): filled $2.57, seen $2.55`,
      'Stop level $1.93 (call, first entry)',
    ]);
    expect.soft(plan.tips.get(sec(7)), 'says a series did not trade in a minute, so a break is explained where it is').toEqual([
      'SPY: no trade this minute',
      `${call}: $2.50`,
      `${put}: no trade this minute`,
      'Starting price $2.00 (call, first entry)',
      'Momentum level $2.50 (call, first entry)',
    ]);
    expect.soft(plan.tips.get(sec(11))?.filter((line) => line.startsWith('Sold')), 'says what a sale was for').toEqual([`Sold ${call} (contracts: 1): filled $1.82, seen $1.84 (Stop)`]);
    expect.soft(plan.tips.get(sec(10))?.filter((line) => line.startsWith('Marked')), 'tells a mark apart from a sale').toEqual([`Marked, not sold: ${put} at $1.25`]);
    expect.soft([...plan.tips.keys()].sort((left, right) => left - right), 'has lines for every minute of the day and for no minute that is not in it').toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12].map(sec));

    const exact = buildTimelinePlan(sessionDetail({ underlying: shownSeries('SPY', null, [[1, '9007199254740993.01'], [2, '1.2345']]) }));
    expect.soft(exact.tips.get(sec(1))?.[0], 'shows a price as the server wrote it, though a number would have lost digits (a price a double cannot hold)').toBe('SPY: $9,007,199,254,740,993.01');
    expect.soft(exact.tips.get(sec(2))?.[0], 'shows a price as the server wrote it, though a number would have lost digits (four decimals)').toBe('SPY: 1.2345');
  });
});

describe('the plan of a day with a series the server withholds', () => {
  it('draws nothing of a withheld contract, no pane, level, fill, mark or tooltip line, and says why in the server’s words', () => {
    const changed = buildTimelinePlan(sessionDetail({ contracts: [withheldSeries(CALL_585.symbol, CALL_585, 'changed'), shownSeries(PUT_585.symbol, PUT_585, [[3, '1.40'], [4, '1.44']])] }));

    expect.soft(changed.panes.map((pane) => pane.caption), 'draws no pane for a withheld contract').toEqual(['SPY', "SPY Oct 16 '26 585 Put"]);
    expect.soft(changed.notDrawn, 'says why a contract is withheld in the server’s words').toEqual([{ key: CALL_585.symbol, what: null, symbol: CALL_585.symbol, ofUnderlying: false, contract: CALL_585, reason: 'changed', message: SERIES_WORDS.changed, listedBelow: true }]);
    expect.soft(changed.series.filter((each) => each.role === 'level'), 'draws none of a withheld contract’s levels').toEqual([]);
    expect.soft(changed.series.filter((each) => each.role === 'fills').map((host) => host.markers.map((marker) => marker.id)), 'draws none of a withheld contract’s fills or marks').toEqual([
      ['fill:SPY261016P00585000:put:0:entry', 'mark:SPY261016P00585000:put:0'],
    ]);
    expect.soft(changed.series.every((each) => each.pane <= 1), 'draws no series on a pane past the one contract that is shown').toBe(true);

    const gone = buildTimelinePlan(sessionDetail({ contracts: [withheldSeries(CALL_585.symbol, CALL_585, 'gone'), shownSeries(PUT_585.symbol, PUT_585, PUT_CLOSES_SHORT)] }));
    expect.soft([...gone.tips.values()].flat().filter((line) => line.includes('Call')), 'keeps nothing of a withheld contract in the tooltip').toEqual([]);

    // The put was marked at 1.25 in the tenth minute and not sold. Its series is withheld, so no pane holds a point for the mark to sit on.
    const marked = buildTimelinePlan(sessionDetail({ contracts: [shownSeries(CALL_585.symbol, CALL_585, CALL_CLOSES), withheldSeries(PUT_585.symbol, PUT_585, 'gone')] }));
    const markers = marked.series.flatMap((each) => each.markers);
    expect.soft(marked.panes.map((pane) => pane.caption), 'draws no pane for a contract whose series is withheld, though its mark is still a fact the day lists').toEqual(['SPY', "SPY Oct 16 '26 585 Call"]);
    expect.soft(markers.filter((marker) => marker.shape === 'square'), 'draws no mark for a contract whose series is withheld').toEqual([]);
    expect.soft(markers.map((marker) => marker.id), 'draws only the shown contract’s fills').toEqual(['fill:SPY261016C00585000:call:0:entry', 'fill:SPY261016C00585000:call:0:exit']);
    expect.soft([...marked.tips.values()].flat().filter((line) => line.startsWith('Marked')), 'puts no mark in a tooltip for a contract whose series is withheld').toEqual([]);
    expect.soft(marked.series.every((each) => each.pane <= 1), 'draws no series on a pane past the one contract that is shown, when the put is withheld').toBe(true);
    expect.soft(marked.notDrawn.map((each) => [each.symbol, each.listedBelow]), 'lists the withheld contract’s mark below, as a fact the day lists').toEqual([[PUT_585.symbol, true]]);
  });

  it('says in the page’s own words that the server gave no message for a withheld series, and says the same of every reason, each in its own words', () => {
    for (const message of [null, '', '   ']) {
      const noMessage = buildTimelinePlan(sessionDetail({ underlying: { ...withheldSeries('SPY', null, 'gone'), message } }));

      expect.soft(noMessage.notDrawn.map((each) => [each.reason, each.message]), `says in the page’s own words, beside the reason it has, that the server gave no message for a series it withheld (message ${JSON.stringify(message)})`).toEqual([['gone', NO_REASON_GIVEN]]);
    }
    for (const reason of ['session_skipped', 'no_pin', 'gone', 'changed', 'not_confirmed', 'unreadable', 'lake_unavailable'] as const) {
      const withReason = buildTimelinePlan(sessionDetail({ underlying: withheldSeries('SPY', null, reason) }));

      expect.soft(withReason.notDrawn, `says the same of every reason, each in its own words (${reason})`).toEqual([{ key: 'SPY', what: null, symbol: 'SPY', ofUnderlying: true, contract: null, reason, message: SERIES_WORDS[reason], listedBelow: false }]);
    }
  });

  it('draws what remains of a day: the contracts when the underlying is withheld, nothing for a skipped day, and says so of a series that holds no minute or whose minutes cannot be placed', () => {
    const noUnderlying = buildTimelinePlan(sessionDetail({ underlying: withheldSeries('SPY', null, 'lake_unavailable') }));
    expect.soft(noUnderlying.panes.map((pane) => pane.caption), 'draws the contracts when the underlying is withheld').toEqual(["SPY Oct 16 '26 585 Call", "SPY Oct 16 '26 585 Put"]);
    expect.soft(noUnderlying.series[0], 'draws the clock first when the underlying is withheld, on the top pane').toMatchObject({ role: 'clock', pane: 0 });
    expect.soft(closesOn(noUnderlying, 0).flatMap(timesOf), 'draws the first contract on the top pane').toEqual([2, 3, 4, 7, 8, 9, 10, 11, 12].map(sec));
    expect.soft(closesOn(noUnderlying, 1).flatMap(timesOf), 'draws the second contract on the pane below').toEqual([3, 4, 5, 6, 10].map(sec));

    const skipped = buildTimelinePlan(skippedSession());
    expect.soft(skipped.series, 'draws no series for a skipped day').toEqual([]);
    expect.soft(skipped.panes, 'draws no pane for a skipped day').toEqual([]);
    expect.soft(skipped.tips.size, 'gives a skipped day no tooltip').toBe(0);
    expect.soft(skipped.notDrawn, 'gives the server’s words for the one series a skipped day sent').toEqual([{ key: 'SPY', what: null, symbol: 'SPY', ofUnderlying: true, contract: null, reason: 'session_skipped', message: SERIES_WORDS.session_skipped, listedBelow: false }]);
    expect.soft(skipped.label, 'names a skipped day’s chart as nothing that could be drawn').toBe('Nothing could be drawn for this day.');

    const noMinutes = buildTimelinePlan(sessionDetail({ contracts: [shownSeries(CALL_585.symbol, CALL_585, [])] }));
    expect.soft(noMinutes.notDrawn, 'draws nothing where a shown series holds no minute, and says so').toEqual([{ key: CALL_585.symbol, what: null, symbol: CALL_585.symbol, ofUnderlying: false, contract: CALL_585, reason: null, message: NO_MINUTES, listedBelow: true }]);

    const outOfOrder = shownSeries(CALL_585.symbol, CALL_585, [[3, '2.00'], [2, '2.10']]);
    const notANumber = shownSeries(CALL_585.symbol, CALL_585, [[2, '2.00'], [3, '0x10']]);
    const twice = shownSeries(CALL_585.symbol, CALL_585, [[2, '2.00'], [2, '2.10']]);

    for (const bad of [outOfOrder, notANumber, twice]) {
      const badPlan = buildTimelinePlan(sessionDetail({ contracts: [bad] }));

      expect.soft(badPlan.panes.map((pane) => pane.caption), 'does not draw a series whose minutes are out of order, whose price is not a number, or that repeats a minute').toEqual(['SPY']);
      expect.soft(badPlan.notDrawn.map((each) => [each.reason, each.message]), 'says so of a series whose minutes are out of order, whose price is not a number, or that repeats a minute').toEqual([[null, UNPLACEABLE]]);
    }
  });
});


describe('the plan of a covered spread: a sold leg’s fills, its stop and its mark (#3109)', () => {
  const plan = buildTimelinePlan(spreadSession());
  const hosts = plan.series.filter((each) => each.role === 'fills');
  const sold = "SPY Oct 16 '26 585 Put";
  const cover = "SPY Oct 16 '26 580 Put";

  it('plans a covered spread: each arrow pointed the way the order traded, the sold leg’s stop above its sale, and the tooltip and the legend in the same words', () => {
    expect.soft(plan.panes.map((pane) => pane.caption), 'names the panes of a covered spread: the underlying, the cover and the sold leg').toEqual(['SPY', cover, sold]);
    expect.soft(hosts.map((host) => host.pane), 'puts the fills of the cover and of the sold leg on their own panes').toEqual([1, 2]);
    // The cover is a bought leg: it reads as a bought leg always has.
    expect.soft(hosts[0].markers, 'reads the cover as a bought leg always has').toEqual([
      { id: 'fill:SPY261016P00580000:long_put:0:entry', time: sec(4), price: 1.42, shape: 'arrowUp', token: 'series-green', text: 'Bought' },
      { id: 'fill:SPY261016P00580000:long_put:0:exit', time: sec(12), price: 1.12, shape: 'arrowDown', token: 'series-orange', text: 'Sold' },
    ]);
    // The sold leg opened by a sale, which points down, and closed by a purchase, which points up. Colour still says
    // whether the fill opened or closed the leg.
    expect.soft(hosts[1].markers, 'points each arrow the way the order traded, and names it by the side: a sale to open down, a buy-back up').toEqual([
      { id: 'fill:SPY261016P00585000:short_put:0:entry', time: sec(4), price: 2.18, shape: 'arrowDown', token: 'series-green', text: 'Sold to open' },
      { id: 'fill:SPY261016P00585000:short_put:0:exit', time: sec(10), price: 3.32, shape: 'arrowUp', token: 'series-orange', text: 'Bought back' },
    ]);

    const stops = plan.series.filter((each) => each.role === 'level');
    expect.soft(stops.map((stop) => [stop.pane, stop.lineStyle, timesOf(stop), valuesOf(stop)]), 'draws a sold leg’s stop from the minute it filled to the minute the buy-back was decided').toEqual([[2, 'large-dashed', [sec(4), sec(9)], [3.27, 3.27]]]);
    const soldAt = hosts[1].markers[0].price;
    expect.soft(valuesOf(stops[0])[0], 'draws a sold leg’s stop above the price it was sold for').toBeGreaterThan(soldAt);

    expect.soft(plan.tips.get(sec(4)), 'says the sale to open in the tooltip in the same words as the legend').toContain(`Sold to open ${sold} (contracts: 1): filled $2.18, seen $2.20`);
    expect.soft(plan.tips.get(sec(4)), 'says the cover’s purchase in the tooltip in the same words as the legend').toContain(`Bought ${cover} (contracts: 1): filled $1.42, seen $1.40`);
    expect.soft(plan.tips.get(sec(10)), 'says the buy-back in the tooltip in the same words as the legend').toContain(`Bought back ${sold} (contracts: 1): filled $3.32, seen $3.30 (Stop)`);
    expect.soft(plan.tips.get(sec(12)), 'says the cover’s sale in the tooltip in the same words as the legend').toContain(`Sold ${cover} (contracts: 1): filled $1.12, seen $1.10 (Exit Time)`);

    expect.soft(plan.panes.map((pane) => pane.entries.map((entry) => `${entry.name} (${entry.drawn})`)), 'names in the legend of each pane the fills it holds, with the arrow each is drawn as').toEqual([
      ['Minute closes (solid line)'],
      ['Minute closes (solid line)', 'Bought (up arrow)', 'Sold (down arrow)'],
      ['Minute closes (solid line)', 'Sold to open (down arrow)', 'Bought back (up arrow)', 'Stop level (long dashes)'],
    ]);
    // A glyph for each marker, pointing the way it traded: a sale down and a purchase up, whichever opened the leg.
    expect.soft(plan.panes[2].entries.filter((entry) => entry.sample === 'marker').map((entry) => entry.glyph), 'gives each marker in the sold leg’s legend a glyph pointing the way it traded').toEqual(['▼', '▲']);
  });

  it('plans a sold leg that was never bought back as a mark that says it is owed, beside the cover that was not sold, and counts an owed mark the page could not place among the marks not drawn', () => {
    const owed = buildTimelinePlan(owedSession());
    const marks = owed.series.filter((each) => each.role === 'fills').flatMap((host) => host.markers).filter((marker) => marker.shape === 'square');

    expect.soft(marks.map((marker) => [marker.id, marker.price, marker.text]), 'draws a sold leg that was never bought back as a mark that says it is owed, beside the cover that was not sold').toEqual([
      ['mark:SPY261016P00580000:long_put:0', 1, 'Marked, not sold'],
      ['mark:SPY261016P00585000:short_put:0', 2.4, 'Marked, owed'],
    ]);
    expect.soft(owed.tips.get(sec(10)), 'says the owed mark in the tooltip').toContain(`Marked, owed: ${sold} at $2.40`);
    expect.soft(owed.tips.get(sec(10)), 'says the cover’s mark in the tooltip').toContain(`Marked, not sold: ${cover} at $1.00`);
    expect.soft(owed.panes.map((pane) => pane.entries.filter((entry) => entry.key.startsWith('mark')).map((entry) => entry.name)), 'says in the legend of each pane which mark it holds').toEqual([
      [],
      ['Marked, not sold'],
      ['Marked, owed'],
    ]);

    const odd = buildTimelinePlan(owedSession({ marks: [{ ...owedSession().marks[1], price_usd: 'NaN' }] }));
    expect.soft(odd.notDrawn.map((each) => [each.what, each.symbol, each.message]), 'counts an owed mark the page could not place among the marks not drawn, by its own name').toEqual([['Marked, owed', PUT_585.symbol, UNPLACEABLE]]);
    expect.soft(PUT_580.symbol, 'keeps the cover’s symbol apart from the sold leg’s').not.toBe(PUT_585.symbol);
  });
});
