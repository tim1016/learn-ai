import type {
  ExplainedCheckView,
  RecentDecisionView,
  StrategyViewCandle,
  StrategyViewResponse,
} from '../../lib/broker-v2-panel.types';
import { orderedChecks } from '../../strategy-view/strategy-view-model';

/**
 * The recent-decisions list's rows (#2639): the panel's decision receipts,
 * oldest first, with the strategy view's newest before-start bar above a
 * "Bot started" divider.
 */

/** One row of the decisions list: a recorded decision, or the newest bar the
 * bot evaluated before it started. */
export interface DecisionRowView {
  readonly key: string;
  readonly timeMs: number;
  /** A bar close lands on a minute; a receipt's own time keeps its seconds. */
  readonly granularity: 'minute' | 'time';
  /** The candle this row selects, when it has one. */
  readonly barCloseMs: number | null;
  readonly beforeStart: boolean;
  /** Backend prose for a before-start bar; rendered verbatim. */
  readonly phaseText: string | null;
  /** Raw codes, rendered through `receiptLabel`. */
  readonly outcome: string | null;
  readonly reasonCode: string | null;
  readonly authorityKind: string | null;
  /** `null` ⇒ this decision was recorded before decisions saved their values. */
  readonly explanation: StrategyViewCandle['explanation'] | null;
  /** The explanation's checks, those that could act on the bar first. */
  readonly checks: readonly ExplainedCheckView[];
}

export type DecisionListEntry =
  | { readonly kind: 'row'; readonly row: DecisionRowView }
  | { readonly kind: 'start'; readonly startedAtMs: number };

function decisionRow(decision: RecentDecisionView): DecisionRowView {
  const barCloseMs = decision.decision_bar_close_ms ?? null;
  return {
    key: `decision:${decision.seq}`,
    timeMs: barCloseMs ?? decision.recorded_at_ms,
    granularity: barCloseMs === null ? 'time' : 'minute',
    barCloseMs,
    beforeStart: false,
    phaseText: null,
    outcome: decision.outcome,
    // "No action · No action" says one thing twice; a reason that only
    // restates its outcome is left out.
    reasonCode: decision.reason_code.toUpperCase() === decision.outcome.toUpperCase() ? null : decision.reason_code,
    authorityKind: decision.authority_kind ?? null,
    explanation: decision.explanation ?? null,
    checks: orderedChecks(decision.explanation?.checks ?? []),
  };
}

function beforeStartRow(candle: StrategyViewCandle): DecisionRowView {
  return {
    key: `before-start:${candle.bar_close_ms}`,
    timeMs: candle.bar_close_ms,
    granularity: 'minute',
    barCloseMs: candle.bar_close_ms,
    beforeStart: true,
    phaseText: candle.phase_text ?? null,
    outcome: candle.outcome ?? null,
    reasonCode: candle.reason_code ?? null,
    authorityKind: null,
    explanation: candle.explanation,
    checks: orderedChecks(candle.explanation.checks),
  };
}

/**
 * The decisions list, oldest first, with the newest before-start bar above a
 * "Bot started" divider.
 *
 * The divider is only drawn where it is true: the panel lists a bounded
 * window of recent decisions, so once the run's first decision has scrolled
 * out of that window a divider above the oldest row would claim the bot
 * started right before a decision it did not start before.
 */
export function decisionListEntries(
  decisions: readonly RecentDecisionView[],
  view: StrategyViewResponse | null,
): DecisionListEntry[] {
  const rows = [...decisions].sort((left, right) => left.seq - right.seq).map(decisionRow);
  const startedAtMs = view?.run_started_at_ms ?? null;
  const beforeStart = view?.candles.filter((candle) => candle.phase === 'before_start').at(-1);
  if (view === null || startedAtMs === null || beforeStart === undefined || !listReachesRunStart(decisions, view)) {
    return rows.map((row) => ({ kind: 'row', row }));
  }
  const firstAfterStart = rows.findIndex((row) => row.timeMs > startedAtMs);
  const insertAt = firstAfterStart === -1 ? rows.length : firstAfterStart;
  return [
    ...rows.slice(0, insertAt).map((row): DecisionListEntry => ({ kind: 'row', row })),
    { kind: 'row', row: beforeStartRow(beforeStart) },
    { kind: 'start', startedAtMs },
    ...rows.slice(insertAt).map((row): DecisionListEntry => ({ kind: 'row', row })),
  ];
}

/**
 * The listed decisions include the run's first decision bar (or the run has
 * none yet). A run with decisions the view leaves out — recorded before
 * decisions saved their values — cannot show which decision came first.
 */
function listReachesRunStart(decisions: readonly RecentDecisionView[], view: StrategyViewResponse): boolean {
  if ((view.unexplained_decision_count ?? 0) > 0) return false;
  const firstDecisionSeq = view.candles.find(
    (candle) => candle.phase === 'decision' && candle.decision_seq !== null && candle.decision_seq !== undefined,
  )?.decision_seq;
  if (firstDecisionSeq === null || firstDecisionSeq === undefined) return true;
  return decisions.some((decision) => decision.seq === firstDecisionSeq);
}
