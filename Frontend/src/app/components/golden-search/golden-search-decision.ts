/**
 * The Final decision step's closed operator copy and its approval rules
 * (#2696). The exam outcome, checks, claim and exposure are the server's; this
 * module only names them and decides which acknowledgements approval needs.
 * Approval needs a written reason and the missing-parity acknowledgement
 * always, and a separate weak-evidence acknowledgement whenever the outcome
 * is not "meets the stated rules" or the claim is not confirmatory — a
 * missing outcome counts as weak, never as a pass.
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

/** True when approving needs the separate acknowledgement of weak research evidence. */
export function weaknessRequired(exam: ExamView): boolean {
  return exam.outcome !== 'meets_rules' || exam.claim !== 'confirmatory';
}

/** What is weak about the final test, in the words the acknowledgement names. */
export function weaknesses(exam: ExamView): string[] {
  const found: string[] = [];
  const failed = exam.checks.filter((check) => check.status === 'fail').map((check) => check.label.toLowerCase());
  switch (exam.outcome) {
    case 'meets_rules':
      break;
    case 'does_not_meet_rules':
      found.push(failed.length > 0 ? `failing the stated rules (${failed.join(', ')})` : 'failing the stated rules');
      break;
    case 'not_enough_evidence':
      found.push('not enough final-test evidence');
      break;
    case 'could_not_evaluate':
      found.push('a final test that could not be evaluated');
      break;
    case null:
      found.push('a final test without an outcome');
      break;
  }
  if (exam.claim !== 'confirmatory') {
    if (exam.exposure_state === 'previously_used') found.push('the previously used test interval');
    else if (exam.exposure_state === 'history_unknown') found.push('the unknown history of this test interval');
    else found.push('an exploratory, not confirmatory, final test');
  }
  return found;
}

/** The reason prefilled only for a confirmatory final test that met the rules — a fact, not an opinion. */
export function factualNote(exam: ExamView): string {
  return weaknessRequired(exam)
    ? ''
    : 'The candidate meets the stated rules on a confirmatory final test. The evidence is Python-only; independent engine agreement is not available.';
}

/** The research row: the outcome, the exposure claim when it is exploratory, and why. */
export function researchRow(exam: ExamView): EvidenceRow {
  const outcome = exam.outcome === null ? 'No outcome recorded' : OUTCOME_LABELS[exam.outcome];
  const exploratory = exam.claim === 'confirmatory' ? '' : ` · ${EXPOSURE_LABELS[exam.exposure_state]}, exploratory`;
  const failing = exam.checks.filter((check) => check.status !== 'pass').map((check) => check.detail);
  const detail = exam.outcome === 'meets_rules' && failing.length === 0 ? 'Observed advantage on this interval; no promise of future profit.' : failing.join(' ') || 'The final test did not produce a judged result.';
  return { title: 'Research evidence', chip: `${outcome}${exploratory}`, tone: weaknessRequired(exam) ? 'warn' : 'good', detail };
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
