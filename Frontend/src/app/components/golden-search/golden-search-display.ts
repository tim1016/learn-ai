/**
 * Display helpers for Golden Search (#2696). They only arrange and label
 * values the server computed — no metric, return or estimate is derived here.
 */

import type { CapabilityKnob, ExposureState, IncumbentRef, Point, PointValue, StrategyCapability } from './golden-search.types';

export interface PointEntry {
  readonly name: string;
  readonly label: string;
  readonly unit: string | null;
  readonly value: PointValue;
}

/** Operator copy for each exposure state; the server's explanation follows it. */
export const EXPOSURE_LABELS: Readonly<Record<ExposureState, string>> = {
  not_opened: 'Not opened in recorded research',
  previously_used: 'Previously used',
  history_unknown: 'History unknown',
};

/** What each exposure state allows the final test to claim. */
export const EXPOSURE_CLAIMS: Readonly<Record<ExposureState, string>> = {
  not_opened: 'The final test can count as a fresh, confirmatory look.',
  previously_used: 'Recorded research already used these dates: the final test can only be exploratory.',
  history_unknown: 'Earlier activity may have touched these dates: the final test can only be exploratory.',
};

/** Where a study's frozen incumbent (its starting point and benchmark) came from. */
export function incumbentLabel(incumbent: IncumbentRef): string {
  return incumbent.source === 'qualification' && incumbent.qualification_id !== null
    ? `Approved qualification ${incumbent.qualification_id.slice(0, 8)}`
    : 'Registry settings';
}

export function knobsByName(capability: StrategyCapability | null): ReadonlyMap<string, CapabilityKnob> {
  return new Map((capability?.knobs ?? []).map((knob) => [knob.name, knob]));
}

/** A point's parameters in the declaration's order (then any others by name), without `symbol`. */
export function pointEntries(point: Point, capability: StrategyCapability | null): PointEntry[] {
  const knobs = knobsByName(capability);
  const declared = [...knobs.keys()].filter((name) => Object.hasOwn(point, name));
  const others = Object.keys(point)
    .filter((name) => name !== 'symbol' && !knobs.has(name))
    .sort();
  return [...declared, ...others].map((name) => {
    const knob = knobs.get(name);
    return { name, label: knob?.label ?? name, unit: knob?.unit ?? null, value: point[name] };
  });
}

/**
 * The parameters on which `point` and `reference` disagree, in the
 * declaration's order, carrying `point`'s value. Canonical points omit
 * identity-neutral defaults, so a key absent from `point` reads as the
 * declared default (null when the declaration does not name one).
 */
export function pointDifferences(point: Point, reference: Point, capability: StrategyCapability | null): PointEntry[] {
  const knobs = knobsByName(capability);
  const valueOf = (source: Point, name: string): PointValue => (Object.hasOwn(source, name) ? source[name] : (knobs.get(name)?.default_value ?? null));
  const union: Record<string, PointValue> = { ...reference, ...point };
  return pointEntries(union, capability)
    .filter(({ name }) => valueOf(point, name) !== valueOf(reference, name))
    .map((entry) => ({ ...entry, value: valueOf(point, entry.name) }));
}

export function entryText(entry: PointEntry): string {
  return `${entry.label} ${entry.value === null ? '—' : String(entry.value)}`;
}

function durationText(seconds: number): string {
  if (seconds < 90) return `${Math.round(seconds)} s`;
  const minutes = seconds / 60;
  if (minutes < 90) return `${Math.round(minutes)} min`;
  return `${(minutes / 60).toFixed(1)} h`;
}

/** The server's serial-time range as words, e.g. `about 40 min – 1.1 h`. */
export function durationRangeText(lowSeconds: number, highSeconds: number): string {
  const low = durationText(lowSeconds);
  const high = durationText(highSeconds);
  return low === high ? `about ${low}` : `about ${low} – ${high}`;
}

/**
 * A fraction as the number a percent input shows (`0.07` → `7`), trimmed of
 * binary noise so a value the trader typed reads back exactly as typed.
 */
export function percentInputValue(fraction: number): number {
  return Number((fraction * 100).toPrecision(12));
}
