import type { TimelinePlan } from '../timeline-plan';

/**
 * Room for a marker's label at the two ends of the one-day chart (#3030). The library centres a marker's text on its
 * minute and draws nothing outside the plot, so with the first and last minute touching the edges a fill in the first
 * minutes read "ought" for "Bought", and a mark in the last minute lost half of "Marked, not sold". The chart is given a
 * range wider than the day instead: on each side as many bars as make as many pixels as the longest label reaches.
 * That is a number of bars that depends on the width of the chart, so it is worked out again whenever the width changes.
 * The axis's own label at an end of the day ("16:00") needs the room too: the library shifts it inside the plot only while
 * the first and last minute are fixed to the edges.
 */

/** About what a character of the chart's own 12 px text is wide, with a pixel over the font's average, so the room is never short. */
const TEXT_PX_PER_CHARACTER = 7;
/** A pixel for the edge of the shape the text hangs from. */
const EDGE_PX = 1;
/** The axis writes the time at an end of the day as well, five characters ("16:00"), however few markers the day has. */
const AXIS_LABEL_CHARACTERS = 5;

/**
 * `value` rounded up to a whole number, for a value of 0 or more. This folder keeps `Math` and `Number` to the one file
 * where a price becomes a number; what is counted here is places on a scale and pixels, and no price comes into it.
 */
function roundedUp(value: number): number {
  const rest = value % 1;
  return rest === 0 ? value : value - rest + 1;
}

/** How far the widest label of the plan reaches to each side of its minute, in pixels: half its width. A marker's text, or the axis's own label. */
export function labelReachPx(plan: TimelinePlan): number {
  let longest = AXIS_LABEL_CHARACTERS;
  for (const series of plan.series) {
    for (const marker of series.markers) {
      if (marker.text.length > longest) longest = marker.text.length;
    }
  }
  return roundedUp((longest * TEXT_PX_PER_CHARACTER) / 2) + EDGE_PX;
}

/** How many places the plan takes on the time scale: one for each instant any series has a point at. */
export function slotCount(plan: TimelinePlan): number {
  const instants = new Set<number>();
  for (const series of plan.series) {
    for (const datum of series.data) instants.add(datum.time);
  }
  return instants.size;
}

/** A range of places on the time scale, as the library takes it: place 0 is the day's first minute. */
export interface RoomedRange {
  readonly from: number;
  readonly to: number;
}

/**
 * The range that shows all `slots` places of the day in a plot `widthPx` wide, with room for `reachPx` of label beside the
 * first and the last. Each end gets `bars` extra places, and the day then takes `slots / (slots + 2 * bars)` of the width,
 * so `bars` is the least whole number with `bars * width / (slots + 2 * bars) >= reach`. Null when there is nothing to make
 * room for, or the plot is too narrow for the room: the day is then fitted to the width as it is.
 */
export function rangeWithRoom(slots: number, widthPx: number, reachPx: number): RoomedRange | null {
  if (slots < 1 || reachPx <= 0 || !(widthPx > 2 * reachPx)) return null;
  const bars = roundedUp((reachPx * slots) / (widthPx - 2 * reachPx));
  // `0 - bars`: the lint on this folder reads a minus sign on a value as turning it into a number.
  return { from: 0 - bars, to: slots - 1 + bars };
}
