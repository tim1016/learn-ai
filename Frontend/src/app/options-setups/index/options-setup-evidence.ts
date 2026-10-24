import type { IndexedApproval, IndexedRun, IndexedSetup, IndexedStudy, OptionsSetupIndex } from './options-setup-index.service';

/**
 * How evidence and saved setups meet: by the exact content each froze, and
 * never by name. A name addresses a draft that can be edited, deleted and
 * reused; a hash is what a run, a search and an approved version were made
 * from. Two names with identical content share every row below, and neither
 * owns it (PRD #3490 R20).
 */

/** The saved setups whose content, as saved now, is exactly this. */
export function namesHolding(index: OptionsSetupIndex, hash: string | null | undefined): readonly IndexedSetup[] {
  if (hash === null || hash === undefined) return [];
  return index.setups.filter((setup) => setup.setup_sha256 === hash);
}

export interface EvidenceOfContent {
  readonly runs: readonly IndexedRun[];
  readonly studies: readonly IndexedStudy[];
  readonly approvals: readonly IndexedApproval[];
}

/** Every run, search and approved version that froze exactly this content. */
export function evidenceHolding(index: OptionsSetupIndex, hash: string | null | undefined): EvidenceOfContent {
  if (hash === null || hash === undefined) return { runs: [], studies: [], approvals: [] };
  return {
    runs: index.runs.filter((run) => run.setup_sha256 === hash),
    studies: index.studies.filter((study) => study.setup_sha256 === hash),
    approvals: index.approvals.filter((approval) => approval.setup_sha256 === hash),
  };
}

/**
 * What the saved setups say of one piece of evidence's content. The three
 * answers are different facts: nothing saved holds this content (it stays
 * reachable by its identity), the content cannot be read, or these names hold
 * it today.
 */
export type ContentHolders =
  | { readonly kind: 'none' }
  | { readonly kind: 'unreadable'; readonly reason: string | null }
  | { readonly kind: 'names'; readonly names: readonly IndexedSetup[] };

export function holdersOf(
  index: OptionsSetupIndex,
  evidence: { readonly setup_sha256?: string | null; readonly unreadable_reason?: string | null },
): ContentHolders {
  if (evidence.setup_sha256 === null || evidence.setup_sha256 === undefined) {
    return { kind: 'unreadable', reason: evidence.unreadable_reason ?? null };
  }
  const names = namesHolding(index, evidence.setup_sha256);
  return names.length === 0 ? { kind: 'none' } : { kind: 'names', names };
}

/** The newest first of whatever is listed: the server lists newest first, and a tie goes by identity. */
export function newestFirst<T extends { readonly created_at_ms: number; readonly id: string | number }>(rows: readonly T[]): T[] {
  return [...rows].sort((a, b) => b.created_at_ms - a.created_at_ms || String(b.id).localeCompare(String(a.id)));
}
