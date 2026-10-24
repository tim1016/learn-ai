import type { GoldenOptionSetup } from '../../../components/options-backtest/golden-option-setups.service';
import { newestFirst } from '../../index/options-setup-evidence';
import type { IndexedApproval, IndexedRun, IndexedStudy, OptionsSetupIndex } from '../../index/options-setup-index.service';

/**
 * One approved version, listed on a setup's Golden tab, and how it came to be there. A version is found by
 * the content it froze, or through the study that began from this setup's content and made it: the second is
 * a changed candidate, which is never shown as an exact match to what is saved now.
 */
export type VersionRelation =
  | { readonly kind: 'content' }
  | { readonly kind: 'study'; readonly study: IndexedStudy };

export interface ListedVersion {
  readonly approval: IndexedApproval;
  readonly relation: VersionRelation;
}

export interface VersionsOfSetup {
  /** Versions that froze exactly the saved content. */
  readonly exact: readonly ListedVersion[];
  /** Versions a search that began from the saved content made with other content: linked through the study, never matched. */
  readonly throughStudy: readonly ListedVersion[];
  /** The searches that began from exactly the saved content. */
  readonly studies: readonly IndexedStudy[];
}

/**
 * The versions of one setup's saved content. `hash` is that content; no name is read. A version whose content is
 * the saved content is exact, whichever way it was approved. A version that a search made, from a study that
 * began from the saved content, with other content, is linked through the study.
 */
export function versionsOfSetup(index: OptionsSetupIndex, hash: string | null | undefined): VersionsOfSetup {
  if (hash === null || hash === undefined) return { exact: [], throughStudy: [], studies: [] };
  const studies = newestFirst(index.studies.filter((study) => study.setup_sha256 === hash));
  const seeded = new Map(studies.map((study) => [study.id, study]));
  const exact: ListedVersion[] = [];
  const throughStudy: ListedVersion[] = [];
  for (const approval of newestFirst(index.approvals)) {
    if (approval.setup_sha256 === hash) {
      exact.push({ approval, relation: { kind: 'content' } });
      continue;
    }
    const study = approval.study_id === null || approval.study_id === undefined ? undefined : seeded.get(approval.study_id);
    if (study !== undefined) throughStudy.push({ approval, relation: { kind: 'study', study } });
  }
  return { exact, throughStudy, studies };
}

/** The completed runs that froze exactly the saved content, newest first: the ones a review can start from. */
export function reviewableRuns(index: OptionsSetupIndex, hash: string | null | undefined): readonly IndexedRun[] {
  if (hash === null || hash === undefined) return [];
  return newestFirst(index.runs.filter((run) => run.setup_sha256 === hash && run.status === 'complete'));
}

/**
 * What the address's `?run=` names, as a run id: a positive whole number that is not larger than a run id can be.
 * Anything else names no run, and nothing is reviewed in its place.
 */
export function reviewedRunId(raw: string | undefined | null): number | null {
  if (raw === undefined || raw === null || !/^[1-9]\d{0,14}$/.test(raw)) return null;
  const id = Number(raw);
  return Number.isSafeInteger(id) ? id : null;
}

/** How a version was made, in words that never call a single run a search. */
export const PATH_TITLE: Readonly<Record<IndexedApproval['origin'], string>> = {
  backtest: 'Single-run approval',
  search: 'Golden search',
  not_recorded: 'Approval path not recorded',
};

export const PATH_EXPLANATION: Readonly<Record<IndexedApproval['origin'], string>> = {
  backtest: 'Approved from one backtest run after review. No golden search ran for it, so no statistical qualification is claimed.',
  search: 'Approved from a golden search: the candidate, and the study’s recorded verdict, are below.',
  not_recorded: 'A study is referenced, but its link is incomplete. No search qualification is claimed for this version.',
};

/** What a search version's recorded verdict says, from its evidence. The verdict is the study's, recorded when it was approved. */
export function verdictWords(evidence: GoldenOptionSetup['evidence']): string | null {
  switch (evidence.verdict) {
    case 'meets_rules':
      return 'The search recorded that this candidate met its rules.';
    case 'owner_exception':
      return 'The owner approved this candidate as an exception: it did not meet every rule, and the failed rules stay with it.';
    default:
      return null;
  }
}
