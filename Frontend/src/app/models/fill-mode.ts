import type { components } from "../api/broker.types";

/**
 * Where a research run fills its market orders — the Grid Search contract's
 * closed vocabulary, which the Strategy Lab engine request accepts too.
 * `decision_minute_open` fills at the open of the first minute at or after the
 * decision bar's close, the earliest price a live order could get (#2599).
 */
export type FillModeName = NonNullable<components["schemas"]["GridSearchSpecRequest"]["fillMode"]>;

/**
 * What a research form starts on: the fill the evidence grade and the
 * Walk-Forward Study use to ask "will this survive live?" (#2599).
 */
export const RESEARCH_DEFAULT_FILL_MODE: FillModeName = "decision_minute_open";

/** Keyed by the contract's vocabulary, so a mode the backend adds cannot go unlabelled. */
const FILL_MODE_LABELS: Record<FillModeName, string> = {
  signal_bar_close: "Signal bar close",
  next_bar_open: "Next bar open",
  decision_minute_open: "Decision minute open",
};

export function isFillModeName(value: unknown): value is FillModeName {
  return typeof value === "string" && Object.hasOwn(FILL_MODE_LABELS, value);
}

export function fillModeLabel(value: FillModeName): string {
  return FILL_MODE_LABELS[value];
}

/** The pickers' options, in the vocabulary's order. */
export const FILL_MODE_OPTIONS: readonly { value: FillModeName; label: string }[] = Object.keys(FILL_MODE_LABELS)
  .filter(isFillModeName)
  .map((value) => ({ value, label: FILL_MODE_LABELS[value] }));
