/**
 * The Final decision step's closed operator copy (#2696). The exam outcome,
 * checks, claim, exposure and weakness are the server's; this module only
 * names them. Approval needs a written reason and the missing-parity
 * acknowledgement always, and a separate weak-evidence acknowledgement
 * whenever the server names a weakness (`exam.weakness`) — a missing outcome
 * is never read as a pass.
 */

import { EXPOSURE_LABELS } from './golden-search-display';
import type { ExamOutcome, ExamView, StudyDetail, StudyState } from './golden-search.types';

export const OUTCOME_LABELS: Readonly<Record<ExamOutcome, string>> = {
  meets_rules: 'Meets the stated rules',
  does_not_meet_rules: 'Does not meet the rules',
  not_enough_evidence: 'Not enough evidence',
  could_not_evaluate: 'Could not evaluate',
};

export type EvidenceTone = 'good' | 'warn' | 'bad' | 'neutral';

export interface EvidenceRow {
  readonly title: string;
  readonly chip: string;
  readonly tone: EvidenceTone;
  readonly detail: string;
}

/** The server's word that the evidence meets the rules: a judged outcome, and no weakness named. */
function meetsRules(exam: ExamView): boolean {
  return exam.outcome !== null && exam.weakness.length === 0;
}

/** The reason prefilled only when the server names no weakness in a judged final test — a fact, not an opinion. */
export function factualNote(exam: ExamView): string {
  return meetsRules(exam)
    ? 'The candidate meets the stated rules on a confirmatory final test. The evidence is Python-only; independent engine agreement is not available.'
    : '';
}

/** The research row: the outcome, the exposure claim when it is exploratory, and why. */
export function researchRow(exam: ExamView): EvidenceRow {
  const outcome = exam.outcome === null ? 'No outcome recorded' : OUTCOME_LABELS[exam.outcome];
  const exploratory = exam.claim === 'confirmatory' ? '' : ` · ${EXPOSURE_LABELS[exam.exposure_state]}, exploratory`;
  const failing = exam.checks.filter((check) => check.status !== 'pass').map((check) => check.detail);
  const detail = exam.outcome === 'meets_rules' && failing.length === 0 ? 'Observed advantage on this interval; no promise of future profit.' : failing.join(' ') || 'The final test did not produce a judged result.';
  return { title: 'Research evidence', chip: `${outcome}${exploratory}`, tone: meetsRules(exam) ? 'good' : 'warn', detail };
}

/** The four separate evidence rows of the decision panel, by study state (closed copy). */
export function evidenceRows(state: StudyState, exam: ExamView): EvidenceRow[] {
  const approved = state === 'approved';
  const repeatability: EvidenceRow =
    state === 'approved'
      ? { title: 'Repeatability and coverage', chip: 'Verified', tone: 'good', detail: 'The immutable replay and required coverage match this exact version.' }
      : state === 'qualification_failed'
        ? { title: 'Repeatability and coverage', chip: 'Proof failed', tone: 'bad', detail: 'The required replay did not complete or did not match. No new qualification was published.' }
        : state === 'qualification_pending'
          ? { title: 'Repeatability and coverage', chip: 'Building the proof', tone: 'warn', detail: 'The immutable replay and required coverage must match this exact version.' }
          : { title: 'Repeatability and coverage', chip: 'Built during approval', tone: 'neutral', detail: 'The immutable replay and required coverage must match this exact version.' };
  return [
    researchRow(exam),
    repeatability,
    {
      title: 'Independent engine agreement',
      chip: 'Missing · Python-only',
      tone: 'warn',
      detail: approved ? 'Missing parity remains recorded after Manual override.' : 'A separate acknowledgement is required for Manual override.',
    },
    approved
      ? { title: 'Golden acceptance', chip: 'Accepted · Manual override', tone: 'good', detail: 'Selectable for Paper and Live; operating checks remain.' }
      : { title: 'Golden acceptance', chip: 'Awaiting your approval', tone: 'warn', detail: 'Approval publishes the qualified version and the active default together.' },
  ];
}

/** The default the approval expects to replace: the frozen incumbent's qualification, or none for registry settings. */
export function expectedDefault(study: Pick<StudyDetail, 'protocol'>): string | null {
  const incumbent = study.protocol.incumbent;
  return incumbent.source === 'qualification' ? incumbent.qualification_id : null;
}
