/**
 * Recency Chart display-mode window logic (design spec D18-D19).
 *
 * All-symbols mode bounds the visible window to roughly one trading week
 * so many minute-scale lanes stay legible; single-symbol mode (exactly
 * one symbol currently toggled on) unlocks the full accumulated history.
 * The mode is DERIVED from the current symbol-toggle selection, never
 * set independently — narrowing to one symbol is what unlocks the long
 * window.
 */

export type DisplayMode = "all-symbols" | "single-symbol";

const ONE_DAY_MS = 24 * 60 * 60_000;
const ALL_SYMBOLS_WINDOW_MS = 7 * ONE_DAY_MS;

/**
 * Fallback span when there's no trade history to anchor single-symbol
 * mode's window — a calendar-day approximation of "about a week", not a
 * trading-session count (nothing to count sessions against yet).
 */
const NO_HISTORY_FALLBACK_MS = 7 * ONE_DAY_MS;

export interface DisplayWindow {
  start: number;
  end: number;
}

export function computeDisplayMode(visibleSymbols: string[]): DisplayMode {
  return visibleSymbols.length === 1 ? "single-symbol" : "all-symbols";
}

export function computeDisplayWindow(
  mode: DisplayMode,
  nowMs: number,
  history: { earliestEntryMs: number | null } = { earliestEntryMs: null },
): DisplayWindow {
  if (mode === "all-symbols") {
    // This is deliberately an elapsed display span, not a claim about
    // exchange sessions. Scheduled-session counts belong to Python's
    // canonical market calendar.
    return { start: nowMs - ALL_SYMBOLS_WINDOW_MS, end: nowMs };
  }
  const start = history.earliestEntryMs ?? nowMs - NO_HISTORY_FALLBACK_MS;
  return { start: Math.min(start, nowMs - 1), end: nowMs };
}
