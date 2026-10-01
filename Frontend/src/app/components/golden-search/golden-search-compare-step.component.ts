import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';
import { ButtonModule } from 'primeng/button';

import { GoldenSearchCandidateAsideComponent } from './golden-search-candidate-aside.component';
import { GoldenSearchCandidateTableComponent } from './golden-search-candidate-table.component';
import { candidateRows, initialRowKey, rowKeyFor, selectedCaption, type CandidateRow } from './golden-search-compare';
import { GoldenSearchEvidenceTabsComponent } from './golden-search-evidence-tabs.component';
import { GoldenSearchRetainComponent, type RetainDecision } from './golden-search-retain.component';
import type { StudyStep } from './golden-search-steps';
import type { CandidateKey, StrategyCapability, StudyCommand, StudyDetail } from './golden-search.types';

/** The one forward action Compare offers for the selected candidate. */
type CompareAction =
  | { readonly kind: 'select'; readonly key: CandidateKey; readonly label: string }
  | { readonly kind: 'step'; readonly label: string }
  | { readonly kind: 'none'; readonly label: string; readonly reason: string };

const INCUMBENT_CAPTION = 'The incumbent is not a new candidate. Use “Keep current settings” to finish without consuming the test.';

/**
 * The Compare step (#2696): the server's recommendation first, then every
 * candidate — the searches' fits and the frozen incumbent — on the same
 * development scope; the selected candidate's settings; its evidence (the
 * parameter landscape, equity, months, trades, neighborhood, stress) beside
 * what choosing it implies; and the two ways forward: keep the current
 * settings, or lock the candidate for its one final test. Picking a row is a
 * local choice until "Review final-test lock" sends it.
 */
@Component({
  selector: 'app-golden-search-compare-step',
  imports: [ButtonModule, GoldenSearchCandidateAsideComponent, GoldenSearchCandidateTableComponent, GoldenSearchEvidenceTabsComponent, GoldenSearchRetainComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-compare-step.component.html',
  styleUrl: './golden-search-compare-step.component.scss',
})
export class GoldenSearchCompareStepComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);
  readonly busy = input(false);
  readonly studyCommand = output<StudyCommand>();
  readonly goTo = output<StudyStep>();

  /** The row the trader clicked, for the study it was clicked in; otherwise the study's own pick shows. */
  private readonly picked = signal<{ readonly studyId: string; readonly key: CandidateKey } | null>(null);

  protected readonly evidence = computed(() => this.study().results.evidence);
  protected readonly rows = computed<CandidateRow[]>(() => candidateRows(this.evidence()?.candidates ?? []));
  protected readonly selectedKey = computed<CandidateKey | null>(() => {
    const rows = this.rows();
    const study = this.study();
    if (study.exam_locked) return rowKeyFor(rows, study.candidate_key);
    const picked = this.picked();
    return rowKeyFor(rows, picked?.studyId === study.id ? picked.key : null) ?? initialRowKey(rows, study.candidate_key);
  });
  protected readonly selectedRow = computed(() => this.rows().find((row) => row.key === this.selectedKey()) ?? null);
  protected readonly caption = computed(() => {
    const row = this.selectedRow();
    return row === null ? null : selectedCaption(row.candidate);
  });
  protected readonly pairMaps = computed(() => {
    const fromEvidence = this.evidence()?.pair_maps ?? [];
    return fromEvidence.length > 0 ? fromEvidence : (this.study().results.search?.pair_maps ?? []);
  });
  protected readonly mapCenter = computed(() => this.study().results.search?.winner ?? null);
  protected readonly mapCenterLabel = computed(() => {
    const allPeriod = this.evidence()?.candidates.find((candidate) => candidate.key === 'all_period');
    return `the ${allPeriod?.label ?? 'all-period fit'}`;
  });
  protected readonly canRetain = computed(() => this.study().permitted_actions.includes('retain'));
  protected readonly pending = computed(() =>
    this.study().state === 'validation_running' ? 'Candidates appear here when the test over time finishes.' : 'No candidate evidence is recorded for this study yet.',
  );

  protected readonly action = computed<CompareAction>(() => {
    const study = this.study();
    const row = this.selectedRow();
    if (study.exam_locked) return { kind: 'step', label: 'Review final decision' };
    if (row === null) return { kind: 'none', label: 'Review final-test lock', reason: 'Choose a candidate first.' };
    if (!row.candidate.exam_eligible) return { kind: 'none', label: 'Review final-test lock', reason: INCUMBENT_CAPTION };
    if (study.state === 'candidate_locked' && study.candidate_key === row.key) return { kind: 'step', label: 'Review final-test lock' };
    if (study.permitted_actions.includes('select_candidate')) return { kind: 'select', key: row.key, label: 'Review final-test lock' };
    return { kind: 'none', label: 'Review final-test lock', reason: study.action_refusals.select_candidate ?? 'This study cannot lock a candidate now.' };
  });

  protected pick(key: CandidateKey): void {
    this.picked.set({ studyId: this.study().id, key });
  }

  protected forward(): void {
    const action = this.action();
    if (action.kind === 'select') this.studyCommand.emit({ command: 'select_candidate', payload: { candidate_key: action.key } });
    else if (action.kind === 'step') this.goTo.emit('decision');
  }

  protected retain(decision: RetainDecision): void {
    this.studyCommand.emit({ command: 'retain', payload: { kind: decision.kind, note: decision.note } });
  }
}
