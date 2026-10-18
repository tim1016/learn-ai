import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';

import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { equityPlot, isLoss, scaledBarPercents } from '../options-backtest/run/run-chart-scale';
import type { OptionsStudy } from './options-golden-search.types';

@Component({
  selector: 'app-options-study-overview',
  imports: [ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './options-study-overview.component.html',
  styleUrl: './options-golden-search.component.scss',
})
export class OptionsStudyOverviewComponent {
  readonly study = input.required<OptionsStudy>();
  protected readonly selectedHash = signal<string | null>(null);
  protected readonly landscape = computed(() => {
    const candidates = this.study().candidates.flatMap((candidate) => candidate.verdict !== 'could_not_evaluate' && candidate.net_profit_usd !== null && candidate.drawdown_usd !== null ? [{ candidate, profit: candidate.net_profit_usd, risk: candidate.drawdown_usd }] : []);
    const gains = equityPlot(candidates.map((row) => row.profit));
    const risks = equityPlot(candidates.map((row) => row.risk));
    if (gains === null || risks === null) return null;
    // ADR 0068: rotate the shared decimal projection for the horizontal risk axis.
    const points = candidates.map((row, index) => ({ ...row, x: (148 - risks.coordinates[index].y) * 1000 / 136, y: gains.coordinates[index].y * 2.25 }));
    return {
      points,
      high: gains.high,
      low: gains.low,
      minRisk: risks.low,
      maxRisk: risks.high,
    };
  });
  protected readonly selected = computed(() => this.landscape()?.points.find((point) => point.candidate.candidate_hash === this.selectedHash()) ?? null);
  protected readonly rejectedCount = computed(() => this.study().rejected_candidate_count ?? this.study().candidates.filter((candidate) => !candidate.is_original && candidate.verdict !== 'meets_rules').length);
  protected readonly rows = computed(() => {
    const candidates = this.study().candidates;
    const widths = scaledBarPercents(candidates.map((candidate) => candidate.verdict === 'could_not_evaluate' ? '0' : candidate.net_profit_usd ?? '0'), 50);
    return candidates.map((candidate, index) => ({ candidate, width: widths[index], loss: isLoss(candidate.net_profit_usd ?? '0'), final: this.study().final_results.find((result) => result.candidate_hash === candidate.candidate_hash) }));
  });
}
