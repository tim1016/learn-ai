/**
 * Display helpers for Golden Search (#2696). They only arrange and label
 * values the server computed — no metric, return or estimate is derived here.
 */

import type { CapabilityKnob, ExposureState, IncumbentRef, Metrics, Point, PointValue, StrategyCapability, StudyDetail } from './golden-search.types';

export interface PointEntry {
  readonly name: string;
  readonly label: string;
  readonly unit: string | null;
  readonly value: PointValue;
}

/** Whether a plan takes each window's trade floor from its expected trade frequency (ADR 0074) instead of two fixed floors. */
export function usesTradeFrequency(protocol: { readonly expected_trades_per_year?: number | null }): boolean {
  return protocol.expected_trades_per_year != null;
}

/**
 * The trade floor the server recorded for a study's development period or
 * final test: the floor its receipt froze under an expected trade frequency,
 * else the plan's fixed floor. Null when the study does not carry it.
 */
export function studyTradeFloor(study: Pick<StudyDetail, 'protocol' | 'activity'>, window: 'development' | 'final'): number | null {
  if (usesTradeFrequency(study.protocol)) return study.activity?.windows.find((item) => item.key === window)?.minimum_trades ?? null;
  return (window === 'final' ? study.protocol.exam_min_trades : study.protocol.policy.min_trades) ?? null;
}

/** Operator copy for each exposure state; the server's explanation follows it. */
export const EXPOSURE_LABELS: Readonly<Record<ExposureState, string>> = {
  not_opened: 'Not opened in recorded research',
  previously_used: 'Previously used',
  history_unknown: 'History unknown',
};

/** What opening the final test would record, as the lock states it: only an untouched interval can be confirmatory. */
export const EXPOSURE_PREVIEW_LABELS: Readonly<Record<ExposureState, string>> = {
  not_opened: EXPOSURE_LABELS.not_opened,
  previously_used: `${EXPOSURE_LABELS.previously_used} · exploratory only`,
  history_unknown: `${EXPOSURE_LABELS.history_unknown} · exploratory only`,
};

/** What opening the final test would record, when the server previewed it (while a candidate is being chosen or locked). */
export function exposurePreview(study: StudyDetail): { readonly label: string; readonly explanation: string } | null {
  const preview = study.exposure_preview;
  return preview === null ? null : { label: EXPOSURE_PREVIEW_LABELS[preview.state], explanation: preview.explanation };
}

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

/**
 * Every declared knob of `point` in the declaration's order, then any other
 * parameter by name, without `symbol`. A canonical point omits a knob at its
 * identity-neutral default, so an absent declared knob reads as that default
 * (`isDefault`); nothing else is filled in.
 */
export function fullPointEntries(point: Point, capability: StrategyCapability | null): (PointEntry & { readonly isDefault: boolean })[] {
  const knobs = capability?.knobs ?? [];
  const declared = knobs.map((knob) => {
    const present = Object.hasOwn(point, knob.name);
    return { name: knob.name, label: knob.label, unit: knob.unit, value: present ? point[knob.name] : knob.default_value, isDefault: !present };
  });
  const names = new Set(knobs.map((knob) => knob.name));
  const others = Object.keys(point)
    .filter((name) => name !== 'symbol' && !names.has(name))
    .sort()
    .map((name) => ({ name, label: name, unit: null, value: point[name], isDefault: false }));
  return [...declared, ...others];
}

const SIGNED_PERCENT = new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1, signDisplay: 'exceptZero' });
const PERCENT = new Intl.NumberFormat('en-US', { style: 'percent', minimumFractionDigits: 1, maximumFractionDigits: 1 });
const RATIO = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const SIGNED_USD = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 0, maximumFractionDigits: 0, signDisplay: 'exceptZero' });
const SIGNED_CENTS = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2, signDisplay: 'exceptZero' });
const CENTS = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** A server fraction as a signed percent (`0.087` → `+8.7%`); undefined reads "—", never zero. */
export function signedPercentText(fraction: number | null | undefined): string {
  return fraction === null || fraction === undefined ? '—' : SIGNED_PERCENT.format(fraction);
}

/** A server fraction as an unsigned percent (`0.064` → `6.4%`), e.g. a drawdown. */
export function percentText(fraction: number | null | undefined): string {
  return fraction === null || fraction === undefined ? '—' : PERCENT.format(fraction);
}

/** A ratio such as Sharpe to two places. */
export function ratioText(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : RATIO.format(value);
}

/** A dollar result with its sign (`+$186`, `−$92`). */
export function signedUsdText(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : SIGNED_USD.format(value);
}

/** A dollar result to the cent with its sign (`+$0.40`, `−$92.15`): a trade, or a result judged at $0. */
export function signedCentsText(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : SIGNED_CENTS.format(value);
}

/** A dollar amount to the cent with no sign (`$590.10`): a price or a width, not a result. */
export function centsText(value: number | null | undefined): string {
  return value === null || value === undefined ? '—' : CENTS.format(value);
}

/** A run's four comparison figures as text, and why they read "—" when its run failed. */
export interface MetricTexts {
  /** Net return with its sign, e.g. `+8.7%`. */
  readonly netReturn: string;
  /** Worst fall from peak, unsigned. */
  readonly worstFall: string;
  readonly trades: string;
  readonly sharpe: string;
  /** A failed run's error ("The run failed." when it gave none); null for a completed run or no result. */
  readonly failure: string | null;
}

const NO_FIGURES = { netReturn: '—', worstFall: '—', trades: '—', sharpe: '—' } as const;

/**
 * The net return, worst fall, trades and Sharpe every Golden Search comparison
 * shows for one run. A failed run or a missing result reads "—" in every
 * figure, as does any statistic the engine left undefined — never zero.
 */
export function metricTexts(metrics: Metrics | null): MetricTexts {
  if (metrics === null) return { ...NO_FIGURES, failure: null };
  if (metrics.status === 'failed') return { ...NO_FIGURES, failure: metrics.error ?? 'The run failed.' };
  return {
    netReturn: signedPercentText(metrics.total_return_pct),
    worstFall: percentText(metrics.max_drawdown_pct),
    trades: String(metrics.total_trades),
    sharpe: ratioText(metrics.sharpe_ratio),
    failure: null,
  };
}
