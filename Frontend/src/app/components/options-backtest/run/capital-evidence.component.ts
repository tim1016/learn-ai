import { ChangeDetectionStrategy, Component, computed, input } from '@angular/core';

import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import type { RunFigures } from '../options-backtest.types';
import { fundedThroughSentence, requirementRuleWords } from '../run-words';

/**
 * Whether the cash could have carried the run, and where that evidence stops
 * (#3108): funded continuously through which session, the event that ended it,
 * and five figures of the run's money, each on its own and none standing in for
 * another. The totals keep every session whatever this says. A run saved before
 * it was kept shows "not recorded", and nothing is worked out here.
 */
@Component({
  selector: 'app-options-backtest-capital-evidence',
  imports: [AuthoredUsdPipe, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './capital-evidence.component.html',
  styleUrl: './capital-evidence.component.scss',
})
export class CapitalEvidenceComponent {
  readonly figures = input.required<RunFigures>();

  protected readonly evidence = computed(() => this.figures().capital_evidence ?? null);
  protected readonly stamps = computed(() => this.figures().stamps ?? null);
  protected readonly funded = computed(() => {
    const evidence = this.evidence();
    return evidence === null ? '' : fundedThroughSentence(evidence);
  });
  protected readonly ruleWords = computed(() => {
    const stamps = this.stamps();
    return stamps === null ? '' : requirementRuleWords(stamps.requirement_rule);
  });
}
