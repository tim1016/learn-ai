import { describe, expect, it } from 'vitest';

import { buildTimelinePlan } from '../timeline-plan';
import { SCENE_FILLS, sessionDetail, skippedSession } from '../testing/detail-fixtures';
import { labelReachPx, rangeWithRoom, slotCount } from './timeline-room';

/**
 * Where the library puts the middle of a place on the time scale, in pixels from the plot's left edge, for a range it was
 * asked to show: its own `indexToCoordinate`, with a bar as wide as the plot over the places in the range.
 */
function centreOf(place: number, range: { from: number; to: number }, widthPx: number): number {
  const barPx = widthPx / (range.to - range.from + 1);
  return widthPx - (range.to - place + 0.5) * barPx - 1;
}

describe('how far a label reaches from its minute', () => {
  it('is half the width of the longest label of the day, at 7 px a character and a pixel over, and never less than the axis’s own label', () => {
    // The toy day has "Bought", "Sold" and "Marked, not sold": 16 characters, 112 px, 56 each side, and the pixel.
    expect.soft(labelReachPx(buildTimelinePlan(sessionDetail())), 'is half the width of the longest label of the day, at 7 px a character and a pixel over').toBe(57);
    // "Bought" is 6 characters: 42 px, 21 each side, and the pixel.
    expect.soft(labelReachPx(buildTimelinePlan(sessionDetail({ marks: [] }))), 'is the longest label that is there: a day of fills alone reaches less than a day with a mark').toBe(22);
    // "16:00" is five characters: 35 px, 18 each side, and the pixel.
    expect.soft(labelReachPx(buildTimelinePlan(sessionDetail({ fills: [], marks: [] }))), 'is never less than the axis’s own label at an end of the day, "16:00"').toBe(19);
    // "Sold" is four characters, and does not need more than the axis does.
    expect.soft(labelReachPx(buildTimelinePlan(sessionDetail({ fills: SCENE_FILLS.filter((fill) => fill.kind === 'exit'), marks: [] }))), 'is never less than the axis’s own label when the longest label is "Sold"').toBe(19);
  });
});

describe('the places a day takes on the time scale', () => {
  it('is one for each minute of the session, and none for a day that was skipped', () => {
    // SPY did not print in the seventh minute, and the put traded in four of the twelve.
    expect.soft(slotCount(buildTimelinePlan(sessionDetail())), 'is one for each minute of the session: the clock holds a place for a minute that no series traded in').toBe(12);
    expect.soft(slotCount(buildTimelinePlan(skippedSession())), 'is none for a day that was skipped, which draws nothing').toBe(0);
  });
});

describe('the range that gives a day room for the labels at its two ends', () => {
  // A normal session is 390 minutes, and the longest label reaches 57 px. Each end gets the least whole number of places
  // with `places * width / (390 + 2 * places) >= 57`: 22230 / (width - 114), rounded up.
  it.each([
    [300, 120, 22230 / 186],
    [600, 46, 22230 / 486],
    [1200, 21, 22230 / 1086],
  ])('is %i px wide: %i places more on each side of the 390 minutes, since the room needed is %f', (widthPx, places) => {
    expect(rangeWithRoom(390, widthPx, 57)).toEqual({ from: -places, to: 389 + places });
  });

  it('holds the label of the first and of the last minute inside the plot, at every width and for every length of day, and takes no more room than the label needs', () => {
    for (const slots of [1, 12, 120, 390, 960]) {
      for (const widthPx of [240, 300, 400, 600, 800, 1000, 1400, 2000, 3000]) {
        const range = rangeWithRoom(slots, widthPx, 57);
        if (range === null) throw new Error(`No room was made for ${slots} places at ${widthPx} px.`);
        // The whole day is still in the range.
        expect.soft(range.from, `keeps the whole day in the range: starts at or before the first place (${slots} places at ${widthPx} px)`).toBeLessThanOrEqual(0);
        expect.soft(range.to, `keeps the whole day in the range: ends at or after the last place (${slots} places at ${widthPx} px)`).toBeGreaterThanOrEqual(slots - 1);
        // Half a label, 56 px, is between the first place and the left edge, and between the last place and the right.
        expect.soft(centreOf(0, range, widthPx), `holds the label of the first minute inside the plot (${slots} places at ${widthPx} px)`).toBeGreaterThanOrEqual(56);
        expect.soft(widthPx - centreOf(slots - 1, range, widthPx), `holds the label of the last minute inside the plot (${slots} places at ${widthPx} px)`).toBeGreaterThanOrEqual(56);
      }
    }

    const roomed = rangeWithRoom(390, 600, 57);
    if (roomed === null) throw new Error('No room was made.');
    const places = -roomed.from - 1;

    expect.soft((places * 600) / (390 + 2 * places), 'takes no more room than the label needs: one place less is short of it').toBeLessThan(57);
  });

  it.each([
    ['there is no label to make room for', 390, 600, 0],
    ['the plot is too narrow for the room', 390, 114, 57],
    ['the plot is narrower than that', 390, 100, 57],
    ['the chart has no width yet', 390, 0, 57],
    ['the chart’s width is not a number', 390, Number.NaN, 57],
    ['the day has no place on the scale', 0, 600, 57],
  ])('is none when %s, and the day is then fitted to the width as it is', (_why, slots, widthPx, reachPx) => {
    expect(rangeWithRoom(slots, widthPx, reachPx)).toBeNull();
  });
});
