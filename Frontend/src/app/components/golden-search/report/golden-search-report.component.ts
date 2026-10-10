import { CurrencyPipe, DecimalPipe } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, signal } from '@angular/core';
import { RouterLink } from '@angular/router';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp';
import { GoldenSearchChanceFinalPanelComponent } from '../golden-search-chance-final-panel.component';
import { GoldenSearchChancePanelComponent } from '../golden-search-chance-panel.component';
import { metricTexts } from '../golden-search-display';
import { GoldenSearchDrawdownPanelComponent } from '../golden-search-drawdown-panel.component';
import { GoldenSearchFinalChartsComponent } from '../golden-search-final-charts.component';
import { GoldenSearchRehearsalPanelComponent } from '../golden-search-rehearsal-panel.component';
import { GoldenSearchTestStepComponent } from '../golden-search-test-step.component';
import type { CandidateKey, StrategyCapability, StudyDetail } from '../golden-search.types';

@Component({
  selector: 'app-golden-search-report',
  imports: [AssetIdentityComponent, CurrencyPipe, DecimalPipe, GoldenSearchChanceFinalPanelComponent, GoldenSearchChancePanelComponent, GoldenSearchDrawdownPanelComponent, GoldenSearchFinalChartsComponent, GoldenSearchRehearsalPanelComponent, GoldenSearchTestStepComponent, ReceiptLabelPipe, RouterLink, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './golden-search-report.component.html',
  styleUrl: './golden-search-report.component.scss',
})
export class GoldenSearchReportComponent {
  readonly study = input.required<StudyDetail>();
  readonly capability = input<StrategyCapability | null>(null);
  protected readonly selected = signal<CandidateKey | null>(null);
  protected readonly chosenFit = computed(() => {
    const exam = this.study().results.exam;
    return exam?.fits.find(fit => fit.key === exam.chosen_key) ?? null;
  });
  protected readonly candidates = computed(() => (this.study().results.evidence?.candidates ?? []).map(candidate => ({ ...candidate, figures: metricTexts(candidate.development_metrics), summary: this.study().decision_summaries.find(item => item.candidate_key === candidate.key)?.rows ?? [] })));
  protected readonly developmentChoices = computed(() => {
    const incumbent = this.candidates().find(candidate => candidate.key === 'incumbent');
    return this.candidates().filter(candidate => candidate.key !== 'incumbent' && candidate.point_hash !== incumbent?.point_hash);
  });
  protected readonly developmentFit = computed(() => this.developmentChoices().find(candidate => candidate.key === (this.selected() ?? this.chosenFit()?.key)) ?? this.developmentChoices()[0] ?? null);
  protected readonly finals = computed(() => {
    const exam = this.study().results.exam;
    return exam === null ? [] : [...exam.fits.map(fit => ({ key: fit.key, label: fit.label, outcome: fit.outcome, missing: fit.metrics === null, figures: metricTexts(fit.metrics) })), { key: 'incumbent', label: 'Frozen incumbent', outcome: null, missing: exam.incumbent_metrics === null, figures: metricTexts(exam.incumbent_metrics) }];
  });
  protected readonly fit = computed(() => {
    const exam = this.study().results.exam;
    return exam?.fits.find(fit => fit.key === (this.selected() ?? exam.chosen_key)) ?? exam?.fits[0] ?? null;
  });
  protected readonly incompatibleRank = computed(() => {
    const carry = this.fit()?.chance.carry_over;
    // #3025: v1 ranks engine Sharpe against session-close, zero-risk-free rehearsals.
    return carry?.status === 'measured' && carry.basis.schema_version === 'golden-search-chance-carry-over/v1' && carry.basis.objective.measure === 'sharpe_ratio';
  });
  protected print(): void { window.print(); }
}
