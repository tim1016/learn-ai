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
const ALL_SYMBOLS_WEEKDAY_COUNT = 5;

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

/**
 * Elapsed ms spanning the last `weekdayCount` weekdays (Mon-Fri) ending at
 * nowMs, inclusive of "today" if it's a weekday. Skips whole weekends
 * rather than treating every elapsed hour as trading time — the prior bug
 * subtracted 5 x 6.5 trading-hours (32.5 elapsed hours) from `now`, which
 * on a Monday afternoon lands on Sunday morning and covers only Monday's
 * own session. This is a frontend-only weekday approximation, not the
 * canonical NYSE calendar (holidays aren't excluded) — acceptable here
 * because this bounds a *display* window, not a numerical or persisted
 * value; see .claude/rules/temporal-rigor.md for what does require the
 * canonical calendar module.
 */
function weekdaySpanMs(nowMs: number, weekdayCount: number): number {
  let cursor = nowMs;
  let remaining = weekdayCount;
  while (remaining > 0) {
    const dayOfWeek = new Date(cursor).getUTCDay(); // 0=Sun, 6=Sat
    if (dayOfWeek !== 0 && dayOfWeek !== 6) remaining--;
    if (remaining > 0) cursor -= ONE_DAY_MS;
  }
  return nowMs - cursor;
}

export function computeDisplayWindow(
  mode: DisplayMode,
  nowMs: number,
  history: { earliestEntryMs: number | null } = { earliestEntryMs: null },
): DisplayWindow {
  if (mode === "all-symbols") {
    return { start: nowMs - weekdaySpanMs(nowMs, ALL_SYMBOLS_WEEKDAY_COUNT), end: nowMs };
  }
  const start = history.earliestEntryMs ?? nowMs - NO_HISTORY_FALLBACK_MS;
  return { start: Math.min(start, nowMs - 1), end: nowMs };
}
