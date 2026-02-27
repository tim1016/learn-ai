import {
  Component,
  signal,
  inject,
  DestroyRef,
  ChangeDetectionStrategy,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of, finalize } from 'rxjs';
import { ResearchService, ResearchExperiment } from '../../../services/research.service';
import { InputText } from 'primeng/inputtext';
import { ButtonModule } from 'primeng/button';
import { TableModule } from 'primeng/table';
import { TagModule } from 'primeng/tag';
import { MessageModule } from 'primeng/message';
import { TooltipModule } from 'primeng/tooltip';
import { DialogModule } from 'primeng/dialog';

interface ColumnHelp {
  icon: string;
  label: string;
  description: string;
}

@Component({
  selector: 'app-experiment-history',
  standalone: true,
  imports: [
    CommonModule,
    FormsModule,
    InputText,
    ButtonModule,
    TableModule,
    TagModule,
    MessageModule,
    TooltipModule,
    DialogModule,
  ],
  templateUrl: './experiment-history.component.html',
  styleUrls: ['./experiment-history.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExperimentHistoryComponent {
  private researchService = inject(ResearchService);
  private destroyRef = inject(DestroyRef);

  ticker = signal('AAPL');
  loading = signal(false);
  experiments = signal<ResearchExperiment[]>([]);
  error = signal<string | null>(null);

  // Help dialog state
  helpVisible = signal(false);
  helpColumn = signal<ColumnHelp | null>(null);

  columnHelp: Record<string, ColumnHelp> = {
    feature: {
      icon: 'pi pi-sliders-h',
      label: 'Feature',
      description: 'The predictive feature (technical indicator) tested against 15-minute forward log returns. Each feature captures a different market signal: momentum measures price velocity, RSI measures overbought/oversold conditions, volatility measures price dispersion, volume z-score measures unusual trading activity, and MACD measures trend-following momentum.',
    },
    meanIC: {
      icon: 'pi pi-chart-line',
      label: 'Mean IC',
      description: 'Information Coefficient — the average daily Spearman rank correlation between feature values and subsequent 15-minute returns. Values above 0.03 indicate meaningful predictive power. Higher magnitude means stronger prediction. IC is the gold standard metric for evaluating alpha factors in quantitative finance.',
    },
    tStat: {
      icon: 'pi pi-percentage',
      label: 't-Stat',
      description: 'The t-statistic tests whether the mean IC is statistically different from zero. A t-stat above 1.65 rejects the null hypothesis (no predictive power) at the 10% significance level. Higher values indicate greater confidence that the feature has genuine predictive ability rather than random correlation.',
    },
    adf: {
      icon: 'pi pi-wave-pulse',
      label: 'ADF p',
      description: 'Augmented Dickey-Fuller test p-value. Tests if the feature time series has a unit root (non-stationary). A p-value below 0.05 means the feature is stationary — it has stable statistical properties over time, making it a reliable predictor. Non-stationary features can produce spurious correlations.',
    },
    monotonic: {
      icon: 'pi pi-sort-amount-up',
      label: 'Monotonic',
      description: 'Quantile monotonicity check. When observations are sorted into 5 bins by feature value, monotonic returns mean higher (or lower) feature values consistently produce higher returns. This confirms a dose-response relationship — the strongest form of predictive evidence, ruling out non-linear artifacts.',
    },
    result: {
      icon: 'pi pi-verified',
      label: 'Result',
      description: 'Overall validation verdict. A feature passes if ALL three tests are met: (1) |Mean IC| > 0.03 with t-stat > 1.65, (2) ADF confirms stationarity (p < 0.05), and (3) quantile returns are monotonic (ratio >= 75%). Only validated features should be considered for live trading signals.',
    },
    dateRange: {
      icon: 'pi pi-calendar',
      label: 'Period',
      description: 'The date range of market data used in this experiment. Longer periods provide more statistical power but may include regime changes. Short periods are noisier but capture recent market dynamics.',
    },
    runDate: {
      icon: 'pi pi-clock',
      label: 'Run Date',
      description: 'When this experiment was executed. Use this to compare results over time — if a feature\'s IC degrades across runs, it may be losing predictive power (alpha decay).',
    },
  };

  loadExperiments(): void {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;

    this.loading.set(true);
    this.error.set(null);

    this.researchService
      .getExperiments(t)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          this.error.set(err?.message ?? 'Failed to load experiments');
          return of([]);
        }),
        finalize(() => this.loading.set(false)),
      )
      .subscribe(exps => this.experiments.set(exps));
  }

  validationSeverity(passed: boolean): 'success' | 'danger' {
    return passed ? 'success' : 'danger';
  }

  validationLabel(passed: boolean): string {
    return passed ? 'PASSED' : 'FAILED';
  }

  showHelp(key: string): void {
    const col = this.columnHelp[key];
    if (col) {
      this.helpColumn.set(col);
      this.helpVisible.set(true);
    }
  }

  icSeverityClass(ic: number): string {
    const abs = Math.abs(ic);
    if (abs >= 0.05) return 'text-green-700 font-bold';
    if (abs >= 0.03) return 'text-green-600 font-semibold';
    return 'text-gray-600';
  }

  tStatSeverityClass(t: number): string {
    if (t >= 1.65) return 'text-green-700 font-bold';
    return 'text-gray-600';
  }

  adfSeverityClass(p: number): string {
    if (p < 0.05) return 'text-green-700';
    return 'text-amber-600';
  }
}
