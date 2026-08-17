import {
  ChangeDetectionStrategy,
  Component,
  computed,
  input,
  signal,
} from '@angular/core';

import type {
  FoldResult,
  ParameterCandidateConfig,
  ParameterSearchConfig,
  TrainingCandidateResult,
} from '../../../../services/walk-forward.types';
import {
  type SelectedFoldOutcome,
  WalkForwardSelectedOutcomesComponent,
} from '../walk-forward-selected-outcomes/walk-forward-selected-outcomes.component';
import { WalkForwardFoldExplainerComponent } from '../walk-forward-fold-explainer/walk-forward-fold-explainer.component';

interface CandidateCell {
  fold: FoldResult;
  training: TrainingCandidateResult | null;
  value: number | null;
  displayValue: string;
  positive: boolean;
  negative: boolean;
  selected: boolean;
}

interface CandidateRow {
  id: string;
  label: string;
  cells: readonly CandidateCell[];
}

type TrainingMetricKey =
  | 'sharpe_ratio'
  | 'total_return_pct'
  | 'max_drawdown_pct'
  | 'total_trades';

type TrainingMetricFormat = 'ratio' | 'percent' | 'integer';

interface TrainingMetricDefinition {
  key: TrainingMetricKey;
  label: string;
  columnLabel: string;
  evidenceLabel: string;
  description: string;
  format: TrainingMetricFormat;
  positiveLegend: string | null;
  negativeLegend: string | null;
}

const TRAINING_METRICS: readonly TrainingMetricDefinition[] = [
  {
    key: 'sharpe_ratio',
    label: 'Sharpe',
    columnLabel: 'Sharpe',
    evidenceLabel: 'training Sharpe',
    description: 'Annualized average daily return divided by daily-return volatility. It measures risk-adjusted consistency, not portfolio growth.',
    format: 'ratio',
    positiveLegend: 'Positive train Sharpe',
    negativeLegend: 'Negative train Sharpe',
  },
  {
    key: 'total_return_pct',
    label: 'Net return',
    columnLabel: 'Return',
    evidenceLabel: 'training net return',
    description: 'Net P&L divided by starting cash for that training window. This is the closest of these four metrics to portfolio growth.',
    format: 'percent',
    positiveLegend: 'Positive net return',
    negativeLegend: 'Negative net return',
  },
  {
    key: 'max_drawdown_pct',
    label: 'Max drawdown',
    columnLabel: 'Drawdown',
    evidenceLabel: 'training max drawdown',
    description: 'Largest peak-to-trough equity decline in the training window. Lower is better; zero means no recorded decline.',
    format: 'percent',
    positiveLegend: null,
    negativeLegend: null,
  },
  {
    key: 'total_trades',
    label: 'Trades',
    columnLabel: 'Trades',
    evidenceLabel: 'training trades',
    description: 'Closed trades recorded in the training window. Use this to judge whether the other metrics rest on enough observations.',
    format: 'integer',
    positiveLegend: null,
    negativeLegend: null,
  },
];

/**
 * Read-only comparison of persisted parameter-search evidence.
 *
 * Candidate cells contain TRAIN metrics only. The separate outcome strip
 * contains the selected candidate's OOS result, preserving the receipt's
 * evidence boundary instead of implying every candidate was tested OOS.
 */
@Component({
  selector: 'app-walk-forward-parameter-comparison',
  imports: [WalkForwardFoldExplainerComponent, WalkForwardSelectedOutcomesComponent],
  templateUrl: './walk-forward-parameter-comparison.component.html',
  styleUrl: './walk-forward-parameter-comparison.component.scss',
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class WalkForwardParameterComparisonComponent {
  readonly folds = input.required<readonly FoldResult[]>();
  readonly parameterSearch = input.required<ParameterSearchConfig>();
  readonly metricOptions = TRAINING_METRICS;
  readonly activeMetricKey = signal<TrainingMetricKey>('sharpe_ratio');
  readonly explainedFold = signal<FoldResult | null>(null);
  readonly activeMetric = computed(() =>
    TRAINING_METRICS.find((metric) => metric.key === this.activeMetricKey())
      ?? TRAINING_METRICS[0],
  );

  readonly parameterKeys = computed(() => candidateParameterKeys(
    this.parameterSearch().candidates,
  ));

  readonly parameterHeading = computed(() => {
    const keys = this.parameterKeys();
    return keys.length === 1 ? parameterName(keys[0]) : 'Candidate';
  });

  readonly rows = computed<readonly CandidateRow[]>(() => {
    const keys = this.parameterKeys();
    const metric = this.activeMetric();
    const candidates = sortCandidates(this.parameterSearch().candidates, keys);
    return candidates.map((candidate) => ({
      id: candidate.strategy_spec_hash,
      label: candidateLabel(candidate.parameters, keys),
      cells: this.folds().map((fold) => {
        const training = fold.training_candidates.find((result) =>
          sameParameters(result.parameters, candidate.parameters)) ?? null;
        const value = training?.train_metrics[metric.key] ?? null;
        return {
          fold,
          training,
          value,
          displayValue: value === null ? '—' : formatMetricValue(value, metric.format),
          positive: training?.eligible === true
            && metric.positiveLegend !== null
            && value !== null
            && value > 0,
          negative: training?.eligible === true
            && metric.negativeLegend !== null
            && value !== null
            && value < 0,
          selected: sameParameters(fold.selected_parameters, candidate.parameters),
        };
      }),
    }));
  });

  readonly hasTrainingEvidence = computed(() =>
    this.folds().some((fold) => fold.training_candidates.length > 0),
  );

  readonly outcomes = computed<readonly SelectedFoldOutcome[]>(() =>
    this.folds().map((fold) => ({
      fold,
      parameterLabel: candidateLabel(fold.selected_parameters, this.parameterKeys()),
    })),
  );
  readonly explainedParameterLabel = computed(() => {
    const fold = this.explainedFold();
    return fold === null
      ? ''
      : candidateLabel(fold.selected_parameters, this.parameterKeys());
  });

  selectMetric(key: TrainingMetricKey): void {
    this.activeMetricKey.set(key);
  }

  explainFold(fold: FoldResult): void {
    this.explainedFold.set(fold);
  }

  cellAriaLabel(row: CandidateRow, cell: CandidateCell): string {
    const foldLabel = `Fold ${cell.fold.fold_index + 1}`;
    if (cell.training === null) {
      return `${foldLabel}, ${row.label}: no persisted training result`;
    }

    const metric = this.activeMetric();
    const metricLabel = cell.value === null
      ? `${metric.evidenceLabel} unavailable`
      : `${metric.evidenceLabel} ${cell.displayValue}`;
    const eligibility = cell.training.eligible ? 'eligible' :
      `ineligible: ${cell.training.ineligibility_reason ?? 'reason unavailable'}`;
    const selection = cell.selected ? ', selected for out-of-sample testing' : '';
    return `${foldLabel}, ${row.label}: ${metricLabel}, ${eligibility}${selection}`;
  }
}

function candidateParameterKeys(candidates: readonly ParameterCandidateConfig[]): string[] {
  return [...new Set(candidates.flatMap((candidate) => Object.keys(candidate.parameters)))]
    .sort();
}

function sortCandidates(
  candidates: readonly ParameterCandidateConfig[],
  keys: readonly string[],
): ParameterCandidateConfig[] {
  if (keys.length !== 1) return [...candidates];
  const key = keys[0];
  return [...candidates].sort((left, right) =>
    (left.parameters[key] ?? 0) - (right.parameters[key] ?? 0));
}

function sameParameters(
  left: Readonly<Record<string, number>>,
  right: Readonly<Record<string, number>>,
): boolean {
  const leftKeys = Object.keys(left);
  const rightKeys = Object.keys(right);
  return leftKeys.length === rightKeys.length
    && leftKeys.every((key) => left[key] === right[key]);
}

function candidateLabel(
  parameters: Readonly<Record<string, number>>,
  keys: readonly string[],
): string {
  if (keys.length === 0) return 'No parameter recorded';
  return keys.map((key) => {
    const value = parameters[key];
    const formatted = Number.isFinite(value) ? formatNumber(value) : '—';
    return key === 'gap_bps'
      ? `${formatted} bps`
      : `${parameterName(key)} ${formatted}`;
  }).join(' · ');
}

function parameterName(key: string): string {
  if (key === 'gap_bps') return 'Gap threshold';
  return key
    .split('_')
    .filter(Boolean)
    .map((part) => part[0].toUpperCase() + part.slice(1))
    .join(' ');
}

function formatNumber(value: number): string {
  return new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 }).format(value);
}

function formatMetricValue(value: number, format: TrainingMetricFormat): string {
  switch (format) {
    case 'percent':
      return new Intl.NumberFormat('en-US', {
        style: 'percent',
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(value);
    case 'integer':
      return new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 }).format(value);
    case 'ratio':
      return new Intl.NumberFormat('en-US', {
        minimumFractionDigits: 2,
        maximumFractionDigits: 2,
      }).format(value);
  }
}
