import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import { BreadcrumbComponent } from '../../shared/breadcrumb/breadcrumb.component';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { extractServerMessage } from '../broker/operation-error';
import { SetupSentencesComponent } from '../options-backtest/cards/setup-sentences.component';
import { OptionsBacktestService } from '../options-backtest/options-backtest.service';
import { RunResultComponent } from '../options-backtest/run/run-result.component';
import { holdersOf } from '../../options-setups/index/options-setup-evidence';
import { OptionsSetupIndexService } from '../../options-setups/index/options-setup-index.service';
import { isOptionsEvidenceKind, optionsEvidenceRoute, optionsSetupRoute, type OptionsEvidenceKind } from '../../options-setups/options-setups.address';
import { OptionsEvidenceService } from './options-evidence.service';
import { OptionsResearchLineageComponent } from './options-research-lineage.component';
import { OptionsHistoricalComparisonComponent } from './options-historical-comparison.component';

@Component({
  selector: 'app-options-evidence-page',
  imports: [BreadcrumbComponent, RouterLink, AssetIdentityComponent, ReceiptLabelPipe, TimestampDisplayComponent, RunResultComponent, SetupSentencesComponent, OptionsResearchLineageComponent, OptionsHistoricalComparisonComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-evidence-page.component.html',
  styleUrl: './options-evidence-page.component.scss',
})
export class OptionsEvidencePageComponent {
  readonly kind = input.required<string>();
  readonly subjectId = input.required<string>();
  readonly view = input<string>();
  protected readonly activeView = computed(() => this.view() ?? 'performance');
  readonly inspected_study_id = input<string>();
  readonly comparator_run_id = input<string>();
  readonly bot_return = input<string>();
  protected readonly botReturn = computed(() => {
    const path = this.bot_return();
    return path && /^\/brokers\/alpaca\/clerks\/[^/?#]+\/accounts\/[^/?#]+\/bots\/[^/?#]+$/.test(path) ? path : null;
  });
  private readonly service = inject(OptionsEvidenceService);
  private readonly backtest = inject(OptionsBacktestService);
  private readonly saved = inject(OptionsSetupIndexService);
  /** Where this evidence opens: by what it froze, with no saved name. */
  protected readonly route = computed(() => (isOptionsEvidenceKind(this.kind()) ? optionsEvidenceRoute(this.kind() as OptionsEvidenceKind, this.subjectId()) : []));
  protected readonly goldenRoute = (id: string) => optionsEvidenceRoute('golden', id);
  protected readonly evidenceRoute = (kind: string, id: string) => (isOptionsEvidenceKind(kind) ? optionsEvidenceRoute(kind, id) : []);
  protected readonly setupRoute = optionsSetupRoute;
  /** An address with a kind that is not run, study or golden names no evidence: it fails in place and reads nothing. */
  protected readonly knownKind = computed(() => isOptionsEvidenceKind(this.kind()));
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
  protected readonly evidence = computed(() => this.workspace.hasValue() ? this.workspace.value() : null);
  protected readonly performance = resource({
    params: () => this.activeView() === 'performance' ? this.evidence()?.source_run_id ?? undefined : undefined,
    loader: ({ params }) => this.backtest.run(params),
  });
  protected readonly setup = resource({
    params: () => this.evidence()?.setup,
    loader: ({ params }) => this.backtest.describe(params),
  });
  protected readonly problem = computed(() => {
    if (!this.knownKind()) return `“${this.kind()}” is not a kind of Options evidence. Evidence is a run, a search study or an approved version.`;
    return this.workspace.error() === undefined ? null
      : extractServerMessage(this.workspace.error(), 'This retained evidence could not be read. Retry when its store is available.');
  });
  protected readonly performanceProblem = computed(() => this.performance.error() === undefined ? null
    : extractServerMessage(this.performance.error(), 'The source replay could not be read. Its identity remains visible above.'));
  protected readonly query = computed(() => ({ inspected_study_id: this.inspected_study_id(), comparator_run_id: this.comparator_run_id(), bot_return: this.botReturn() }));
  protected readonly lineageQuery = computed(() => ({ ...this.query(), view: 'lineage' }));
  protected readonly comparisonQuery = computed(() => ({ ...this.query(), view: 'comparison' }));
  protected readonly comparisonContext = computed(() => ({ inspected_study_id: this.inspected_study_id(), bot_return: this.botReturn() }));
}
