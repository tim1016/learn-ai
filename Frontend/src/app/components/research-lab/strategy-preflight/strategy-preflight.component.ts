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
import { TickerRangePickerComponent } from '../../../shared/ticker-range-picker/ticker-range-picker.component';
import type {
  Resolution,
  TickerRange,
} from '../../../shared/ticker-range-picker/ticker-range-picker.types';
import { TICKER_POOL, RECENT_TICKERS } from '../../../shared/ticker-catalog';

type Severity = 'ok' | 'warning' | 'blocking';
type SessionFilter = 'rth_only' | 'full_session' | 'unspecified';
type Timeframe = '5m' | '15m' | '1h';

/** Picker emits ``{ resolution, multiplier }``; the preflight route still
 *  takes a single ``timeframe`` string from a closed set. This adapter is
 *  the per-call seam — it does NOT live in ``utils/ticker-wire`` because
 *  the preflight route deliberately did not migrate to ``TickerRequest``
 *  (its shape is different — see PR (ii) audit). */
function rangeToPreflightTimeframe(r: TickerRange): Timeframe {
  const mult = r.multiplier ?? 1;
  const res: Resolution = r.resolution;
  if (res === 'minute' && mult === 5) return '5m';
  if (res === 'minute' && mult === 15) return '15m';
  if (res === 'hour' && mult === 1) return '1h';
  // Picker may emit a combination outside the preflight's vocabulary
  // (e.g. minute × 60). Throw with a clear message rather than silently
  // coercing — the consumer surfaces this as the run-blocked state.
  throw new Error(
    `Picker emitted (${res} × ${mult}) — preflight only accepts 5m / 15m / 1h. ` +
      `Choose minute×5, minute×15, or hour×1 in the Sampling card.`,
  );
}

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
  imports: [CommonModule, FormsModule, TickerRangePickerComponent],
  templateUrl: './strategy-preflight.component.html',
  styleUrls: ['./strategy-preflight.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyPreflightComponent {
  private readonly http = inject(HttpClient);

  // Form state — pre-populated with the canonical EMA-crossover example.
  // The picker's resolution+multiplier maps to the preflight route's
  // ``timeframe`` ('5m' / '15m' / '1h') via ``rangeToPreflightTimeframe``.
  // Defaults preserve pre-migration behavior (timeframe '15m' = minute × 15).
  readonly strategyName = signal('spy_ema_crossover');
  readonly range = signal<TickerRange>({
    symbol: 'SPY',
    from: '2024-03-28',
    to: '2026-03-27',
    resolution: 'minute',
    multiplier: 15,
  });
  readonly tickerPool = TICKER_POOL;
  readonly recentTickers = RECENT_TICKERS;
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
  /** Multiplier values surfaced in the Sampling card. The picker maps
   *  each (resolution, multiplier) pair onto the preflight's timeframe
   *  vocabulary via ``rangeToPreflightTimeframe`` — only minute×5,
   *  minute×15, and hour×1 round-trip; any other combination throws at
   *  request time. */
  readonly availableMultipliers: readonly number[] = [1, 5, 15, 60];
  readonly availableResolutions: readonly Resolution[] = ['minute', 'hour'];
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
      const r = this.range();
      let timeframe: Timeframe;
      try {
        timeframe = rangeToPreflightTimeframe(r);
      } catch (e) {
        this.errorMessage.set(e instanceof Error ? e.message : String(e));
        return;
      }
      const url = `${environment.pythonServiceUrl}/research/data-divergence/preflight`;
      const body = {
        strategy_name: this.strategyName(),
        symbol: r.symbol,
        // Preflight route uses start_date/end_date (its own shape — NOT
        // a TickerRequest inheritor; see PR (ii) audit).
        start_date: r.from,
        end_date: r.to,
        timeframe,
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
