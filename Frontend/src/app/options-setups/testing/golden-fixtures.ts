import type { GoldenOptionSetup } from '../../components/options-backtest/golden-option-setups.service';
import type { EvidenceWorkspace } from '../../components/options-evidence/options-evidence.service';
import { RUN_WORKSPACE } from '../../components/options-evidence/testing';
import { goldenSetup } from '../../components/options-backtest/testing/golden-fixture';
import { sha } from './fixtures';

/** What the retained-evidence read says of one run: the content it froze (`digit` names it) and nothing it was ever approved as. */
export function runEvidence(runId: number, digit: string, changes: Partial<EvidenceWorkspace> = {}): EvidenceWorkspace {
  return {
    ...RUN_WORKSPACE,
    subject_id: String(runId),
    source_run_id: runId,
    name: `Run ${runId} as recorded`,
    setup_sha256: sha(digit),
    evidence_identity: `run:${runId}:${sha(digit)}`,
    approvals: [],
    ...changes,
  };
}

/** One approved version as read by its own id; `digit` names the content it froze. */
export function versionOf(id: string, digit: string, changes: Partial<GoldenOptionSetup> = {}): GoldenOptionSetup {
  return goldenSetup({ id, setup_sha256: sha(digit), ...changes });
}

/** The evidence a search-made version carries: the study, its manifest and the verdict it recorded. */
export function searchEvidence(studyId: string, verdict: 'meets_rules' | 'owner_exception' = 'meets_rules'): GoldenOptionSetup['evidence'] {
  return {
    ...goldenSetup().evidence,
    options_search_study_id: studyId,
    study_manifest_sha256: 'e'.repeat(64),
    verdict,
    ...(verdict === 'owner_exception'
      ? {
          failed_rules: [{ gate_id: 'final:sharpe', status: 'does_not_meet_rules', actual: '0.4', required: '1.0', reason: 'The final Sharpe hurdle was not met.' }],
          waived_gate_ids: ['final:sharpe'],
          exposure: { classification: 'previously_used', explanation: 'The final interval was used in recorded research.' },
        }
      : {}),
  };
}
