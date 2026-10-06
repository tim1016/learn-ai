/**
 * The Final decision step's closed operator copy (#2696). Each fit's outcome,
 * checks and weakness, and the test's claim and exposure, are the server's;
 * this module only names them. Approval needs a written reason and the
 * missing-parity acknowledgement always, and a separate weak-evidence
 * acknowledgement whenever the server names a weakness of the fit being
 * approved (`fit.weakness`) — a missing outcome is never read as a pass.
 */

import { EXPOSURE_LABELS } from './golden-search-display';
import type { CandidateKey, ExamFit, ExamOutcome, ExamView, StudyDetail, StudyState } from './golden-search.types';

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

/** The final test has a result: every fit that ran has its outcome. */
export function examScored(exam: ExamView | null): exam is ExamView {
  return exam !== null && exam.fits.length > 0 && exam.fits.every((fit) => fit.outcome !== null);
}

/** The fit a key names, by its own key or as a fit with the same settings. */
export function fitFor(exam: ExamView, key: CandidateKey | null): ExamFit | null {
  return key === null ? null : (exam.fits.find((fit) => fit.key === key || fit.same_as.includes(key)) ?? null);
}

/** A fit's outcome in words; a fit not yet scored says so. */
export function outcomeLabel(fit: ExamFit): string {
  return fit.outcome === null ? 'No outcome recorded' : OUTCOME_LABELS[fit.outcome];
}

/** The server's word that the fit's evidence meets the rules: a judged outcome, and no weakness named. */
export function meetsRules(fit: ExamFit): boolean {
  return fit.outcome !== null && fit.weakness.length === 0;
}

/** The reason prefilled only when the server names no weakness in the fit's judged final test — a fact, not an opinion. */
export function factualNote(fit: ExamFit): string {
  return meetsRules(fit) ? `The ${fit.label} meets the stated rules on a confirmatory final test. The evidence is Python-only; independent engine agreement is not available.` : '';
}

/** The research row: the fit's outcome, the exposure claim when it is exploratory, and why. */
export function researchRow(exam: ExamView, fit: ExamFit): EvidenceRow {
  const exploratory = exam.claim === 'confirmatory' ? '' : ` · ${EXPOSURE_LABELS[exam.exposure_state]}, exploratory`;
  const failing = fit.checks.filter((check) => check.status !== 'pass').map((check) => check.detail);
  const detail = fit.outcome === 'meets_rules' && failing.length === 0 ? 'Observed advantage on this interval; no promise of future profit.' : failing.join(' ') || 'The final test did not produce a judged result.';
  return { title: 'Research evidence', chip: `${outcomeLabel(fit)}${exploratory}`, tone: meetsRules(fit) ? 'good' : 'warn', detail };
}

/** The four separate evidence rows of the decision panel for one fit, by study state (closed copy). */
export function evidenceRows(state: StudyState, exam: ExamView, fit: ExamFit): EvidenceRow[] {
  const approved = state === 'approved';
  // Kept or closed: no approval ran and none can follow.
  const ended = state === 'retained' || state === 'closed';
  const repeatability: EvidenceRow =
    state === 'approved'
      ? { title: 'Repeatability and coverage', chip: 'Verified', tone: 'good', detail: 'The immutable replay and required coverage match this exact version.' }
      : state === 'qualification_failed'
        ? { title: 'Repeatability and coverage', chip: 'Proof failed', tone: 'bad', detail: 'The required replay did not complete or did not match. No new qualification was published.' }
        : state === 'qualification_pending'
          ? { title: 'Repeatability and coverage', chip: 'Building the proof', tone: 'warn', detail: 'The immutable replay and required coverage must match this exact version.' }
          : ended
            ? { title: 'Repeatability and coverage', chip: 'Not built', tone: 'neutral', detail: 'The study ended without an approval, so no proof was built.' }
            : { title: 'Repeatability and coverage', chip: 'Built during approval', tone: 'neutral', detail: 'The immutable replay and required coverage must match this exact version.' };
  return [
    researchRow(exam, fit),
    repeatability,
    {
      title: 'Independent engine agreement',
      chip: 'Missing · Python-only',
      tone: 'warn',
      detail: approved ? 'Missing parity remains recorded after Manual override.' : ended ? 'No override was recorded: the study ended without an approval.' : 'A separate acknowledgement is required for Manual override.',
    },
    approved
      ? { title: 'Golden acceptance', chip: 'Accepted · Manual override', tone: 'good', detail: 'Selectable for Paper and Live; operating checks remain.' }
      : ended
        ? { title: 'Golden acceptance', chip: 'Not approved', tone: 'neutral', detail: 'The study ended without publishing a version; the current default stands.' }
        : { title: 'Golden acceptance', chip: 'Awaiting your approval', tone: 'warn', detail: 'Approval publishes the qualified version and the active default together.' },
  ];
}

/** The default the approval expects to replace: the frozen incumbent's qualification, or none for registry settings. */
export function expectedDefault(study: Pick<StudyDetail, 'protocol'>): string | null {
  const incumbent = study.protocol.incumbent;
  return incumbent.source === 'qualification' ? incumbent.qualification_id : null;
}
