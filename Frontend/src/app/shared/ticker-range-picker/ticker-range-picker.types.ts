/**
 * Types for the shared <app-ticker-range-picker>.
 *
 * Designed to be a single source of truth for both Engine Lab and Data
 * Lab — every place that picks a (symbol, date-range, resolution) triple
 * imports ``TickerRange`` from here, so swapping one page's picker never
 * drifts the payload shape.
 */

export type Resolution = "minute" | "hour" | "daily";

/** Trading session filter. ``rth`` = regular hours (09:30–16:00 ET);
 *  ``extended`` = pre + post-market (04:00–20:00 ET). The Engine Lab
 *  consumer treats Extended as a UI preview today — the Python engine
 *  still hardcodes RTH (see ``PythonDataService/app/routers/engine.py``). */
export type Session = "rth" | "extended";

export interface TickerRange {
  symbol: string;
  /** YYYY-MM-DD */
  from: string;
  /** YYYY-MM-DD */
  to: string;
  resolution: Resolution;
  /** Bar multiplier (e.g. 5 for 5-minute bars). Defaults to 1.
   *  The Sampling card only renders a multiplier picker when the host
   *  passes a non-empty ``availableMultipliers`` input. Existing
   *  consumers (data-lab, lean-engine) leave this undefined; the wire
   *  adapter normalizes ``undefined`` to ``1`` at the seam. */
  multiplier?: number;
  /** Defaults to ``rth`` when absent. */
  session?: Session;
  autoFetch?: boolean;
}

export interface TickerOption {
  symbol: string;
  /** Display label. Falls back to the symbol when nothing names it. */
  name: string;
  exchange?: string;
  /** YYYY-MM-DD of the first day the lake holds for this symbol, or null
   *  when unknown. A bound, not a density claim — sessions inside
   *  [firstHeld, lastHeld] may be absent. */
  firstHeld?: string | null;
  /** YYYY-MM-DD of the last day the lake holds, or null when unknown.
   *  Anchors the window proposed when the current one holds no data. */
  lastHeld?: string | null;
}

export type AdvisoryKind = "suggest" | "bad";

export interface AdvisoryAction {
  label: string;
  /** Partial patch applied to the ``TickerRange`` value on click. */
  patch: Partial<TickerRange>;
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

export function daysBetween(a: string, b: string): number {
  return Math.round(
    (new Date(b).getTime() - new Date(a).getTime()) / 86_400_000
  );
}

/**
 * Compute the advisory bundle for a given picker state.
 *
 * Each advisory is a ticker-range smart-combination hint — "minute bars
 * over 90 days is a lot of data, switch to hour" — plus an optional
 * one-click patch the user applies by clicking the action button.
 *
 * Advisories are classified by severity so they render in the right
 * colour: ``suggest`` (blue) and ``bad`` (red).
 */
export function computeAdvisories(state: Readonly<TickerRange>): Advisory[] {
  const out: Advisory[] = [];
  if (state.from === "" || state.to === "") return out;
  const spanDays = daysBetween(state.from, state.to);

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

  if (state.resolution === "minute" && spanDays > 365) {
    out.push({
      kind: "bad",
      icon: "pi pi-exclamation-circle",
      body: `Minute bars over <strong>${spanDays} days</strong> may exceed 250k rows. Split the range or downsample.`,
    });
  }

  return out;
}
