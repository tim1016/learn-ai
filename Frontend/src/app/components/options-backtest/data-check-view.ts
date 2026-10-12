import type { DataProblem, SessionCheck } from './options-backtest.types';

/**
 * What a Check data answer says, put in the order the owner reads it. Every
 * code stays the server's code (the panel shows it through the shared label
 * helper) and every message stays as served; nothing here words a verdict.
 */

/** The sessions that share one reason code, with the server's message for it. */
export interface ReasonGroup {
  readonly code: DataProblem['code'];
  /** The server's messages for this code, each once, in the order met. */
  readonly messages: readonly string[];
  /** The lake's own reason codes, when it recorded a failure, each once. */
  readonly lakeReasons: readonly string[];
  /** The sessions that cannot be tested for this reason, by their open, in range order. */
  readonly sessionOpensMs: readonly number[];
}

/** The reasons the sessions that cannot be tested give, grouped by code, in the order each code is first met. */
export function reasonGroups(sessions: readonly SessionCheck[]): readonly ReasonGroup[] {
  const groups = new Map<DataProblem['code'], { messages: Set<string>; lakeReasons: Set<string>; opens: number[] }>();
  for (const session of sessions) {
    if (session.verdict !== 'cannot_be_tested') continue;
    for (const problem of session.problems) {
      const group = groups.get(problem.code) ?? { messages: new Set<string>(), lakeReasons: new Set<string>(), opens: [] };
      group.messages.add(problem.message);
      if (problem.lake_reason != null) group.lakeReasons.add(problem.lake_reason);
      if (!group.opens.includes(session.session_open_ms)) group.opens.push(session.session_open_ms);
      groups.set(problem.code, group);
    }
  }
  return [...groups].map(([code, group]) => ({
    code,
    messages: [...group.messages],
    lakeReasons: [...group.lakeReasons],
    sessionOpensMs: group.opens,
  }));
}

/** One thing a download must bring for a session. `detail` is the number of contracts, or that it is not yet known. */
export interface MissingPart {
  readonly code: SessionCheck['missing'][number];
  readonly detail: string | null;
}

/** What a session that needs a download is missing; the contracts say how many, or that it is not yet known. */
export function missingParts(session: SessionCheck): readonly MissingPart[] {
  return session.missing.map((code) => ({ code, detail: code === 'contracts' ? contractsDetail(session.contracts) : null }));
}

/** The counts are absent exactly while the state is `not_yet_known`. */
function contractsDetail(needs: SessionCheck['contracts']): string {
  if (needs.missing != null && needs.needed != null) return `${needs.missing} of ${needs.needed} missing`;
  return 'not yet known';
}

/** The sessions with this verdict, in range order. */
export function sessionsWith(sessions: readonly SessionCheck[], verdict: SessionCheck['verdict']): readonly SessionCheck[] {
  return sessions.filter((session) => session.verdict === verdict);
}

/** The sessions that are ready and will be skipped: the window does not fit that day, or the underlying did not trade. */
export function skippedSessions(sessions: readonly SessionCheck[]): readonly SessionCheck[] {
  return sessions.filter((session) => session.verdict === 'ready' && session.skip != null);
}
