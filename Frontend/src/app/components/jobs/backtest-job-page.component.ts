import {
  ChangeDetectionStrategy,
  Component,
  inject,
  signal,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { ButtonModule } from 'primeng/button';
import { InputTextModule } from 'primeng/inputtext';
import { JobsService } from '../../services/jobs.service';
import { JobProgressComponent } from './job-progress.component';

interface BacktestResult {
  success: boolean;
  ticker: string;
  total_trades: number;
  win_rate: number;
  total_pnl_pct: number;
  sharpe_ratio: number;
  max_drawdown_pct: number;
  bars_processed: number;
  error?: string;
}

/**
 * Minimal demo page for the SSE job system. Submits a rule-based
 * backtest, renders inline progress, and shows summary stats when the
 * job completes. Lives at `/jobs-demo`.
 */
@Component({
  selector: 'app-backtest-job-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [FormsModule, ButtonModule, InputTextModule, JobProgressComponent],
  styles: [`
    :host { display: block; max-width: 720px; }
    .form-grid {
      display: grid;
      grid-template-columns: repeat(2, 1fr);
      gap: 0.75rem 1rem;
      margin-bottom: 1rem;
    }
    .field { display: flex; flex-direction: column; gap: 0.25rem; }
    label { font-size: 0.8rem; color: var(--text-color-secondary, #64748b); }
    h2 { margin: 0 0 0.25rem; }
    p.lede { color: var(--text-color-secondary, #64748b); margin: 0 0 1.25rem; }
    .progress-slot { margin: 1rem 0; }
    .result {
      margin-top: 1rem;
      padding: 1rem 1.25rem;
      border: 1px solid var(--surface-border, #e2e8f0);
      border-radius: 8px;
      background: var(--surface-50, #f8fafc);
    }
    .result-grid {
      display: grid;
      grid-template-columns: repeat(3, 1fr);
      gap: 0.75rem 1rem;
      margin-top: 0.5rem;
    }
    .result-grid div { display: flex; flex-direction: column; }
    .result-grid span:first-child {
      font-size: 0.75rem;
      color: var(--text-color-secondary, #64748b);
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    .result-grid span:last-child { font-weight: 600; }
  `],
  template: `
    <h2>Backtest job demo</h2>
    <p class="lede">
      Runs a rule-based EMA-crossover backtest through the SSE job system. Watch the inline
      progress here and in the global jobs drawer (bottom-right).
    </p>

    <div class="form-grid">
      <div class="field">
        <label for="ticker">Ticker</label>
        <input pInputText id="ticker" [(ngModel)]="ticker" />
      </div>
      <div class="field">
        <label for="multiplier">Bar size (minutes)</label>
        <input pInputText id="multiplier" type="number" [(ngModel)]="multiplier" />
      </div>
      <div class="field">
        <label for="from">From date</label>
        <input pInputText id="from" [(ngModel)]="fromDate" />
      </div>
      <div class="field">
        <label for="to">To date</label>
        <input pInputText id="to" [(ngModel)]="toDate" />
      </div>
    </div>

    <p-button
      label="Start backtest"
      icon="pi pi-play"
      (onClick)="run()"
      [disabled]="busy()"
    />

    @if (currentJobId()) {
      <div class="progress-slot">
        <app-job-progress [jobId]="currentJobId()!" />
      </div>
    }

    @if (result(); as r) {
      <div class="result">
        <strong>Result</strong>
        @if (r.error) {
          <p style="color: var(--red-600,#dc2626);">{{ r.error }}</p>
        } @else {
          <div class="result-grid">
            <div><span>Trades</span><span>{{ r.total_trades }}</span></div>
            <div><span>Win rate</span><span>{{ (r.win_rate * 100).toFixed(1) }}%</span></div>
            <div><span>Total PnL</span><span>{{ (r.total_pnl_pct * 100).toFixed(2) }}%</span></div>
            <div><span>Sharpe</span><span>{{ r.sharpe_ratio.toFixed(2) }}</span></div>
            <div><span>Max DD</span><span>{{ (r.max_drawdown_pct * 100).toFixed(2) }}%</span></div>
            <div><span>Bars</span><span>{{ r.bars_processed }}</span></div>
          </div>
        }
      </div>
    }
  `,
})
export class BacktestJobPageComponent {
  private jobs = inject(JobsService);

  ticker = 'SPY';
  fromDate = '2026-01-02';
  toDate = '2026-01-31';
  multiplier = 15;

  readonly currentJobId = signal<string | null>(null);
  readonly result = signal<BacktestResult | null>(null);
  readonly busy = signal(false);

  async run(): Promise<void> {
    this.busy.set(true);
    this.result.set(null);

    const id = await this.jobs.startJob('backtest', {
      ticker: this.ticker,
      fromDate: this.fromDate,
      toDate: this.toDate,
      multiplier: Number(this.multiplier),
      timespan: 'minute',
      parameters: {
        fast_ema_period: 5,
        slow_ema_period: 10,
        rsi_period: 14,
        rsi_min: 50,
        rsi_max: 70,
        min_ema_gap: 0.20,
        exit_bars: 5,
      },
    });
    this.currentJobId.set(id);

    // Poll the registry until terminal, then fetch the result.
    const checkDone = async () => {
      const job = this.jobs.job(id);
      if (!job) return;
      if (job.status === 'completed') {
        try {
          const r = await this.jobs.fetchResult<BacktestResult>(id);
          this.result.set(r);
        } finally {
          this.busy.set(false);
        }
        return;
      }
      if (job.status === 'failed' || job.status === 'cancelled') {
        this.busy.set(false);
        return;
      }
      setTimeout(checkDone, 500);
    };
    setTimeout(checkDone, 500);
  }
}
