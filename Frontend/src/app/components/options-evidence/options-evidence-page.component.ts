import { ChangeDetectionStrategy, Component, computed, inject, input, resource } from '@angular/core';
import { RouterLink } from '@angular/router';

import { AssetIdentityComponent } from '../../shared/asset-identity';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import { extractServerMessage } from '../broker/operation-error';
import { SetupSentencesComponent } from '../options-backtest/cards/setup-sentences.component';
import { OptionsBacktestService } from '../options-backtest/options-backtest.service';
import { RunResultComponent } from '../options-backtest/run/run-result.component';
import { OptionsEvidenceService } from './options-evidence.service';

@Component({
  selector: 'app-options-evidence-page',
  imports: [RouterLink, AssetIdentityComponent, ReceiptLabelPipe, TimestampDisplayComponent, RunResultComponent, SetupSentencesComponent],
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
  private readonly service = inject(OptionsEvidenceService);
  private readonly backtest = inject(OptionsBacktestService);
  protected readonly workspace = resource({
    params: () => ({ kind: this.kind(), id: this.subjectId(), study: this.inspected_study_id(), comparator: this.comparator_run_id() }),
    loader: ({ params }) => this.service.read(params.kind, params.id, params.study, params.comparator),
  });
  protected readonly evidence = computed(() => this.workspace.hasValue() ? this.workspace.value() : null);
  protected readonly performance = resource({
    params: () => this.activeView() === 'performance' ? this.evidence()?.source_run_id ?? undefined : undefined,
    loader: ({ params }) => this.backtest.run(params),
  });
  protected readonly setup = resource({
    params: () => this.evidence()?.setup,
    loader: ({ params }) => this.backtest.describe(params),
  });
  protected readonly problem = computed(() => this.workspace.error() === undefined ? null
    : extractServerMessage(this.workspace.error(), 'This retained evidence could not be read. Retry when its store is available.'));
  protected readonly performanceProblem = computed(() => this.performance.error() === undefined ? null
    : extractServerMessage(this.performance.error(), 'The source replay could not be read. Its identity remains visible above.'));
  protected readonly query = computed(() => ({ inspected_study_id: this.inspected_study_id(), comparator_run_id: this.comparator_run_id() }));
  protected readonly lineageQuery = computed(() => ({ ...this.query(), view: 'lineage' }));
}
