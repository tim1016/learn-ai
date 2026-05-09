import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { firstValueFrom } from 'rxjs';

import { environment } from '../../../../environments/environment';
import { PolygonDateRangeComponent } from '../../../shared/polygon-date-range';

type Severity = 'ok' | 'warning' | 'blocking';
type SessionFilter = 'rth_only' | 'full_session' | 'unspecified';
type Timeframe = '5m' | '15m' | '1h';

interface IndicatorEntry {
  name: string;
  length: number;
}

interface PreflightCheck {
  id: string;
  label: string;
  status: Severity;
  message: string;
  fix_hint: string | null;
  docs_link: string | null;
}

interface PreflightResponse {
  overall: Severity;
  summary: string;
  checks: PreflightCheck[];
}

@Component({
  selector: 'app-strategy-preflight',
  standalone: true,
  imports: [CommonModule, FormsModule, PolygonDateRangeComponent],
  templateUrl: './strategy-preflight.component.html',
  styleUrls: ['./strategy-preflight.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyPreflightComponent {
  private readonly http = inject(HttpClient);

  // Form state — pre-populated with the canonical EMA-crossover example.
  readonly strategyName = signal('spy_ema_crossover');
  readonly symbol = signal('SPY');
  readonly fromDate = signal('2024-03-28');
  readonly toDate = signal('2026-03-27');
  readonly timeframe = signal<Timeframe>('15m');
  readonly sessionFilter = signal<SessionFilter>('unspecified');
  readonly warmupDays = signal<number>(0);
  readonly dividendAdjustment = signal<boolean>(false);

  // Indicator list (mutable). Defaults match the SpyEmaCrossoverAlgorithm.
  readonly indicators = signal<IndicatorEntry[]>([
    { name: 'ema', length: 5 },
    { name: 'ema', length: 10 },
    { name: 'rsi', length: 14 },
  ]);
  readonly newIndicatorName = signal('ema');
  readonly newIndicatorLength = signal<number>(20);

  // Server response
  readonly result = signal<PreflightResponse | null>(null);
  readonly isLoading = signal(false);
  readonly errorMessage = signal<string | null>(null);

  // UI conveniences
  readonly timeframes: readonly Timeframe[] = ['5m', '15m', '1h'];
  readonly sessionFilterOptions: readonly { value: SessionFilter; label: string }[] = [
    { value: 'rth_only', label: 'Regular trading hours only (matches TradingView default)' },
    { value: 'full_session', label: 'Full session — pre-market + RTH + after-hours' },
    { value: 'unspecified', label: 'Not declared (engine will use its default)' },
  ];
  readonly indicatorTypes: readonly string[] = [
    'ema', 'sma', 'rsi', 'macd', 'bb', 'adx', 'atr', 'supertrend',
  ];

  readonly canRun = computed(() => !this.isLoading());

  readonly verdictBanner = computed<{ text: string; severity: Severity } | null>(() => {
    const r = this.result();
    if (!r) return null;
    return { text: r.summary, severity: r.overall };
  });

  addIndicator(): void {
    const name = this.newIndicatorName();
    const length = this.newIndicatorLength();
    if (!name || !length || length < 1) return;
    this.indicators.update(list => [...list, { name, length }]);
  }

  removeIndicator(idx: number): void {
    this.indicators.update(list => list.filter((_, i) => i !== idx));
  }

  async runPreflight(): Promise<void> {
    this.isLoading.set(true);
    this.errorMessage.set(null);
    this.result.set(null);
    try {
      const url = `${environment.pythonServiceUrl}/research/data-divergence/preflight`;
      const body = {
        strategy_name: this.strategyName(),
        symbol: this.symbol(),
        start_date: this.fromDate(),
        end_date: this.toDate(),
        timeframe: this.timeframe(),
        indicators: this.indicators(),
        session_filter: this.sessionFilter(),
        warmup_days: this.warmupDays(),
        dividend_adjustment: this.dividendAdjustment(),
      };
      const response = await firstValueFrom(this.http.post<PreflightResponse>(url, body));
      this.result.set(response);
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.errorMessage.set(`Preflight call failed: ${msg}`);
    } finally {
      this.isLoading.set(false);
    }
  }

  iconFor(status: Severity): string {
    switch (status) {
      case 'ok':       return '✓';
      case 'warning':  return '!';
      case 'blocking': return '✕';
    }
  }
}
