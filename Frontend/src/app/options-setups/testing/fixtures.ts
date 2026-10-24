import type { SavedSetup } from '../../components/options-backtest/options-backtest.types';
import { asServed, exampleBody } from '../../components/options-backtest/testing/fixtures';
import type { IndexedApproval, IndexedRun, IndexedStudy, OptionsSetupIndex } from '../index/options-setup-index.service';

/** A content hash as the server writes one: 64 lowercase hex digits. A spec names a content by one repeated digit. */
export function sha(digit: string): string {
  return digit.repeat(64);
}

/** One saved name as the library lists it, holding the content `digit` names (`null`: it can no longer be opened). */
export function savedName(name: string, digit: string | null, changes: Partial<SavedSetup> = {}): SavedSetup {
  return digit === null
    ? { name, example: false, saved_at_ms: 1_760_000_000_000, setup: null, setup_sha256: null, cannot_open_reason: 'legs.0.contracts: Input should be at most 1000', ...changes }
    : { name, example: false, saved_at_ms: 1_760_000_000_000, setup: asServed(exampleBody()), setup_sha256: sha(digit), cannot_open_reason: null, ...changes };
}

export function runOf(id: number, digit: string | null, changes: Partial<IndexedRun> = {}): IndexedRun {
  return {
    id,
    recorded_name: null,
    underlying: 'SPY',
    setup_sha256: digit === null ? null : sha(digit),
    unreadable_reason: digit === null ? 'legs: Field required' : null,
    created_at_ms: 1_791_800_000_000 + id,
    status: 'complete',
    first_session_open_ms: 1_791_379_800_000,
    last_session_open_ms: 1_791_552_600_000,
    sessions_requested: 3,
    net_profit_usd: '1250.40',
    ...changes,
  };
}

export const STUDY_ID = `os_${'a'.repeat(40)}`;

export function studyOf(digit: string | null, changes: Partial<IndexedStudy> = {}): IndexedStudy {
  return {
    id: STUDY_ID,
    recorded_name: 'Alpha',
    underlying: 'SPY',
    setup_sha256: digit === null ? null : sha(digit),
    unreadable_reason: digit === null ? 'The frozen protocol cannot be read.' : null,
    created_at_ms: 1_791_700_000_000,
    updated_at_ms: 1_791_700_100_000,
    phase: 'compare',
    status: 'completed',
    ...changes,
  };
}

export const GOLDEN_ID = `ogs_${'b'.repeat(32)}`;

export function approvalOf(digit: string, changes: Partial<IndexedApproval> = {}): IndexedApproval {
  return {
    id: GOLDEN_ID,
    recorded_name: 'Alpha',
    underlying: 'SPY',
    setup_sha256: sha(digit),
    source_run_id: 7,
    study_id: null,
    origin: 'backtest',
    status: 'ready',
    created_at_ms: 1_791_900_000_000,
    ...changes,
  };
}

export function indexOf(parts: {
  runs?: IndexedRun[];
  studies?: IndexedStudy[];
  approvals?: IndexedApproval[];
  totals?: Partial<Pick<OptionsSetupIndex, 'runs_total' | 'studies_total' | 'approvals_total'>>;
}): OptionsSetupIndex {
  const { runs = [], studies = [], approvals = [] } = parts;
  return {
    read_at_ms: 1_792_000_000_000,
    runs,
    runs_total: runs.length,
    studies,
    studies_total: studies.length,
    approvals,
    approvals_total: approvals.length,
    ...parts.totals,
  };
}
