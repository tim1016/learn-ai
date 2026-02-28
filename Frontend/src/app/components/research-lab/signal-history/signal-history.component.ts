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
import { ResearchService, SignalExperiment } from '../../../services/research.service';
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
  selector: 'app-signal-history',
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
  templateUrl: './signal-history.component.html',
  styleUrls: ['./signal-history.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class SignalHistoryComponent {
  private researchService = inject(ResearchService);
  private destroyRef = inject(DestroyRef);

  ticker = signal('AAPL');
  loading = signal(false);
  experiments = signal<SignalExperiment[]>([]);
  error = signal<string | null>(null);

  helpVisible = signal(false);
  helpColumn = signal<ColumnHelp | null>(null);

  columnHelp: Record<string, ColumnHelp> = {
    feature: {
      icon: 'pi pi-sliders-h',
      label: 'Feature',
      description:
        'The signal feature tested through the walk-forward engine. Each feature captures a different market signal (momentum, RSI, volatility, etc.) and is evaluated for out-of-sample predictive power.',
    },
    grade: {
      icon: 'pi pi-star',
      label: 'Grade',
      description:
        'Overall graduation grade (A–F) based on walk-forward OOS Sharpe, parameter stability, worst-window resilience, and other criteria. A/B grades indicate production-ready signals.',
    },
    status: {
      icon: 'pi pi-flag',
      label: 'Status',
      description:
        'Graduation status label: "Production-Ready", "Paper-Trade", "Exploratory", or "Reject". Only Production-Ready signals should be deployed to live trading.',
    },
    meanOosSharpe: {
      icon: 'pi pi-chart-line',
      label: 'Mean OOS Sharpe',
      description:
        'Average out-of-sample Sharpe ratio across walk-forward windows. Values above 0.5 suggest meaningful risk-adjusted returns. This is the primary metric for signal quality.',
    },
    bestThreshold: {
      icon: 'pi pi-filter',
      label: 'Best Threshold',
      description:
        'The optimal z-score threshold selected during walk-forward optimization. Controls how selective the signal is — higher thresholds reduce trades but may improve quality.',
    },
    period: {
      icon: 'pi pi-calendar',
      label: 'Period',
      description:
        'The date range of market data used in this signal experiment. Longer periods provide more walk-forward windows and statistical power.',
    },
    runDate: {
      icon: 'pi pi-clock',
      label: 'Run Date',
      description:
        'When this signal experiment was executed. Compare results over time to detect alpha decay or changing market conditions.',
    },
  };

  loadExperiments(): void {
    const t = this.ticker().trim().toUpperCase();
    if (!t) return;

    this.loading.set(true);
    this.error.set(null);

    this.researchService
      .getSignalExperiments(t)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          this.error.set(err?.message ?? 'Failed to load signal experiments');
          return of([]);
        }),
        finalize(() => this.loading.set(false)),
      )
      .subscribe(exps => this.experiments.set(exps));
  }

  statusSeverity(label: string): 'success' | 'warn' | 'danger' | 'info' {
    switch (label) {
      case 'Production-Ready':
        return 'success';
      case 'Paper-Trade':
        return 'info';
      case 'Exploratory':
        return 'warn';
      default:
        return 'danger';
    }
  }

  gradeSeverityClass(grade: string): string {
    if (grade === 'A' || grade === 'B') return 'text-green-700 font-bold';
    if (grade === 'C') return 'text-amber-600 font-semibold';
    return 'text-red-600';
  }

  sharpeSeverityClass(sharpe: number): string {
    if (sharpe >= 1.0) return 'text-green-700 font-bold';
    if (sharpe >= 0.5) return 'text-green-600 font-semibold';
    if (sharpe >= 0) return 'text-gray-600';
    return 'text-red-600';
  }

  showHelp(key: string): void {
    const col = this.columnHelp[key];
    if (col) {
      this.helpColumn.set(col);
      this.helpVisible.set(true);
    }
  }

  openReport(id: number): void {
    window.open(`/research-lab/signal-report/${id}`, '_blank');
  }
}
