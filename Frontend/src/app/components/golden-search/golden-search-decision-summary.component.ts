import { ChangeDetectionStrategy, Component, input, output } from '@angular/core';

import type { DecisionSummaryRow, SummaryLink } from './golden-search.types';

const STATUS_TEXT: Readonly<Record<DecisionSummaryRow['status'], string>> = {
  meets: 'Meets',
  concern: 'Concern',
  missing: 'Missing',
};

const STATUS_ICON: Readonly<Record<DecisionSummaryRow['status'], string>> = {
  meets: 'pi-check-circle',
  concern: 'pi-exclamation-triangle',
  missing: 'pi-question-circle',
};

/**
 * What the research supports for one candidate (#2811), beside its evidence:
 * one row per kind of evidence, as the server classified it. A row the study
 * did not record reads Missing, never Meets; each row links to its evidence.
 */
@Component({
  selector: 'app-golden-search-decision-summary',
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-decision-summary.component.html',
  styleUrl: './golden-search-decision-summary.component.scss',
})
export class GoldenSearchDecisionSummaryComponent {
  readonly rows = input.required<readonly DecisionSummaryRow[]>();
  readonly follow = output<SummaryLink>();

  protected readonly statusText = STATUS_TEXT;
  protected readonly statusIcon = STATUS_ICON;
}
