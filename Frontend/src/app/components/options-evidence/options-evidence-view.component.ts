import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { extractServerMessage } from '../broker/operation-error';
import { SetupSentencesComponent } from '../options-backtest/cards/setup-sentences.component';
import { OptionsBacktestService } from '../options-backtest/options-backtest.service';
import type { GoldenOffer } from '../options-backtest/run/golden-offer';
import { RunResultComponent } from '../options-backtest/run/run-result.component';
import { holdersOf } from '../../options-setups/index/options-setup-evidence';
import { OptionsSetupIndexService } from '../../options-setups/index/options-setup-index.service';
import { isOptionsEvidenceKind, optionsEvidenceRoute, optionsSetupRoute, optionsStudyRoute, type OptionsEvidenceKind } from '../../options-setups/options-setups.address';
import { OptionsEvidenceService } from './options-evidence.service';
import { OptionsHistoricalComparisonComponent } from './options-historical-comparison.component';
import { OptionsResearchLineageComponent } from './options-research-lineage.component';

/**
 * One piece of retained Options evidence read exactly as it was frozen: what it
 * tested, what it found, its performance, its research lineage and a comparison
 * with another run. It reads the evidence by its own identity (a run, a search
 * or an approved version), never by a saved name, and says which saved setups
 * hold the same content today.
 *
 * The evidence page holds it at the evidence's own address. A setup's Explore
 * holds it for one of that setup's runs, with its own way between the three
 * views (`embedded`), so the links the view draws lead back to Explore with
 * what Explore's address says besides the view (`keep`).
 */
@Component({
  selector: 'app-options-evidence-view',
  imports: [RouterLink, AssetIdentityComponent, ReceiptLabelPipe, TimestampDisplayComponent, RunResultComponent, SetupSentencesComponent, OptionsResearchLineageComponent, OptionsHistoricalComparisonComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-evidence-view.component.html',
  styleUrl: './options-evidence-view.component.scss',
})
export class OptionsEvidenceViewComponent {
  readonly kind = input.required<string>();
  readonly subjectId = input.required<string>();
  readonly view = input<string>();
  readonly inspected_study_id = input<string>();
  readonly comparator_run_id = input<string>();
  /** The in-app bot this evidence was opened from, already checked to be one; links keep it. */
  readonly botReturn = input<string | null>(null);
  /** Where the view's own links lead: the address of the page that holds it. Absent: the evidence's own address. */
  readonly route = input<readonly string[] | null>(null);
  /** What the holding page's address says besides the view, kept on every link the view draws to it. */
  readonly keep = input<Readonly<Record<string, string>>>({});
  /** The holding page draws its own way between Performance, Research lineage and Historical comparison, so the view draws none. */
  readonly embedded = input(false);
  /** What a run may be taken to Golden as from here: a link, or why not. Absent: Golden is not offered here. */
  readonly goldenOffer = input<GoldenOffer | null>(null);

  private readonly service = inject(OptionsEvidenceService);
  private readonly backtest = inject(OptionsBacktestService);
  private readonly saved = inject(OptionsSetupIndexService);

  protected readonly activeView = computed(() => this.view() ?? 'performance');
  /** An address with a kind that is not run, study or golden names no evidence: it fails in place and reads nothing. */
  protected readonly knownKind = computed(() => isOptionsEvidenceKind(this.kind()));
  /** Where this view's own links lead: the holding page's address, else where this evidence opens by what it froze, with no saved name. */
  protected readonly address = computed<readonly string[]>(() => this.route() ?? (this.knownKind() ? optionsEvidenceRoute(this.kind() as OptionsEvidenceKind, this.subjectId()) : []));
  protected readonly goldenRoute = (id: string) => optionsEvidenceRoute('golden', id);
  protected readonly studyRoute = optionsStudyRoute;
  protected readonly evidenceRoute = (kind: string, id: string) => (isOptionsEvidenceKind(kind) ? optionsEvidenceRoute(kind, id) : []);
  protected readonly setupRoute = optionsSetupRoute;

  protected readonly workspace = resource({
    params: () => (this.knownKind() ? { kind: this.kind(), id: this.subjectId(), study: this.inspected_study_id() } : undefined),
    loader: ({ params }) => this.service.read(params.kind, params.id, params.study),
  });
  /** The saved setups, read once, to say which of them hold this evidence's exact content today. */
  private readonly savedSetups = resource({ loader: () => this.saved.read() });
  protected readonly holders = computed(() => {
    const item = this.evidence();
    if (item === null || !this.savedSetups.hasValue()) return null;
    return holdersOf(this.savedSetups.value(), { setup_sha256: item.setup_sha256 });
  });
  protected readonly holdersUnread = computed(() => this.savedSetups.error() !== undefined);
  protected readonly evidence = computed(() => (this.workspace.hasValue() ? this.workspace.value() : null));
  protected readonly performance = resource({
    params: () => (this.activeView() === 'performance' ? (this.evidence()?.source_run_id ?? undefined) : undefined),
    loader: ({ params }) => this.backtest.run(params),
  });
  protected readonly setup = resource({
    params: () => this.evidence()?.setup,
    loader: ({ params }) => this.backtest.describe(params),
  });
  protected readonly problem = computed(() => {
    if (!this.knownKind()) return `“${this.kind()}” is not a kind of Options evidence. Evidence is a run, a search study or an approved version.`;
    return this.workspace.error() === undefined
      ? null
      : extractServerMessage(this.workspace.error(), 'This retained evidence could not be read. Retry when its store is available.');
  });
  protected readonly performanceProblem = computed(() =>
    this.performance.error() === undefined ? null : extractServerMessage(this.performance.error(), 'The source replay could not be read. Its identity remains visible above.'),
  );
  protected readonly query = computed(() => ({ ...this.keep(), inspected_study_id: this.inspected_study_id(), comparator_run_id: this.comparator_run_id(), bot_return: this.botReturn() }));
  protected readonly lineageQuery = computed(() => ({ ...this.query(), view: 'lineage' }));
  protected readonly comparisonQuery = computed(() => ({ ...this.query(), view: 'comparison' }));
  protected readonly comparisonContext = computed(() => ({ ...this.keep(), inspected_study_id: this.inspected_study_id(), bot_return: this.botReturn() }));
}
