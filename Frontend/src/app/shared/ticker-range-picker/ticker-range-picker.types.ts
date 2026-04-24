/**
 * Types for the shared <app-ticker-range-picker>.
 *
 * Designed to be a single source of truth for both Engine Lab and Data
 * Lab — every place that picks a (symbol, date-range, resolution) triple
 * imports ``TickerRange`` from here, so swapping one page's picker never
 * drifts the payload shape.
 */

export type Resolution = "minute" | "hour" | "daily";

export interface TickerRange {
  symbol: string;
  /** YYYY-MM-DD */
  from: string;
  /** YYYY-MM-DD */
  to: string;
  resolution: Resolution;
  autoFetch?: boolean;
}

export interface TickerOption {
  symbol: string;
  name: string;
  exchange?: string;
  /** Cache completeness fraction 0..1 — colours the per-row hint and
   *  drives the recent/sort order in the combobox. Optional. */
  cache?: number;
  /** YYYY-MM-DD of the last cached day, or null if nothing cached.
   *  Used for "jump to last 30 days of cache" on pick. */
  last?: string | null;
}

export type AvailabilityStatus =
  | "complete"
  | "partial"
  | "missing"
  | "hole"
  | "weekend";

export interface AvailabilityCell {
  date: string;
  status: AvailabilityStatus;
}

export interface AvailabilitySummary {
  complete: number;
  partial: number;
  hole: number;
  missing: number;
  weekdays: number;
}

export type AdvisoryKind = "suggest" | "warn" | "bad" | "info";

export interface AdvisoryAction {
  label: string;
  /** Partial patch to apply to the ``TickerRange`` value on click. */
  patch?: Partial<TickerRange>;
  /** Side-effect flags that the host component interprets (the picker
   *  itself only changes ``TickerRange`` fields). */
  triggerRun?: boolean;
  refetchHoles?: boolean;
}

export interface Advisory {
  kind: AdvisoryKind;
  /** PrimeIcon class, e.g. "pi pi-exclamation-triangle". */
  icon: string;
  /** Rendered via ``innerHTML`` — host code must only ever produce
   *  trusted strings here (the picker never builds advisories from
   *  user input, only from well-known templates). */
  body: string;
  action?: AdvisoryAction;
}

export function summarizeAvailability(
  cells: readonly AvailabilityCell[]
): AvailabilitySummary {
  let complete = 0;
  let partial = 0;
  let hole = 0;
  let missing = 0;
  let weekdays = 0;
  for (const c of cells) {
    if (c.status === "weekend") continue;
    weekdays++;
    if (c.status === "complete") complete++;
    else if (c.status === "partial") partial++;
    else if (c.status === "hole") hole++;
    else missing++;
  }
  return { complete, partial, hole, missing, weekdays };
}

export function daysBetween(a: string, b: string): number {
  return Math.round(
    (new Date(b).getTime() - new Date(a).getTime()) / 86_400_000
  );
}

export function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10);
}

/**
 * Compute the advisory bundle for a given picker state.
 *
 * Each advisory is a ticker-range smart-combination hint — "minute bars
 * over 90 days is a lot of data, switch to hour" — plus an optional
 * one-click patch the user applies by clicking the action button.
 *
 * Advisories are classified by severity so they render in the right
 * colour: ``suggest`` (blue), ``warn`` (amber), ``bad`` (red), ``info``
 * (cyan).
 */
export function computeAdvisories(
  state: Readonly<TickerRange>,
  summary: AvailabilitySummary
): Advisory[] {
  const out: Advisory[] = [];
  const spanDays = daysBetween(state.from, state.to);
  const missingTotal = summary.missing + summary.hole;

  if (state.resolution === "minute" && spanDays > 90) {
    const rows = (spanDays * 390).toLocaleString();
    out.push({
      kind: "suggest",
      icon: "pi pi-forward",
      body: `<strong>${spanDays} days</strong> × minute bars ≈ <span class="mono">${rows}</span> rows. Consider switching to <strong>hour</strong> bars — same signal shape, ~60× faster.`,
      action: { label: "Switch to hour", patch: { resolution: "hour" } },
    });
  }

  if (state.resolution === "hour" && spanDays > 365) {
    out.push({
      kind: "suggest",
      icon: "pi pi-forward",
      body: `<strong>${spanDays} days</strong> × hour bars is large. <strong>Daily</strong> bars render instantly.`,
      action: { label: "Switch to daily", patch: { resolution: "daily" } },
    });
  }

  if (missingTotal > 0 && !state.autoFetch) {
    const plural = missingTotal === 1 ? "" : "s";
    out.push({
      kind: "warn",
      icon: "pi pi-exclamation-triangle",
      body: `<span class="mono">${missingTotal}</span> weekday${plural} not on disk. Enable auto-fetch to pull from Polygon before running.`,
      action: {
        label: "Enable auto-fetch & run",
        patch: { autoFetch: true },
        triggerRun: true,
      },
    });
  }

  if (summary.hole > 2) {
    out.push({
      kind: "bad",
      icon: "pi pi-times-circle",
      body: `<span class="mono">${summary.hole}</span> days cached but incomplete (likely halted or partial bars). Refetch recommended.`,
      action: { label: "Refetch holes", refetchHoles: true },
    });
  }

  if (
    summary.complete === 0 &&
    summary.partial === 0 &&
    summary.weekdays > 0
  ) {
    out.push({
      kind: "info",
      icon: "pi pi-info-circle",
      body: `No local cache for <strong>${state.symbol}</strong> in this range. First run will fetch everything from Polygon.`,
    });
  }

  if (state.resolution === "minute" && spanDays > 365) {
    out.push({
      kind: "bad",
      icon: "pi pi-exclamation-circle",
      body: `Minute bars over <strong>${spanDays} days</strong> may exceed 250k rows. Split the range or downsample.`,
    });
  }

  return out;
}
