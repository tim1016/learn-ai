import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  input,
  output,
  signal,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { RouterModule } from '@angular/router';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../../../environments/environment';

type Severity = 'ok' | 'warning' | 'blocking';
type SessionFilter = 'rth_only' | 'full_session';
type PriceAdjustment = 'unadjusted' | 'split_only' | 'split_and_dividend';
type BarTimestamp = 'bar_open' | 'bar_close';
type Timeframe = '5m' | '15m' | '1h';

interface IndicatorEntry { name: string; length: number; }

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

/**
 * TV-compatibility panel for the Engine Lab.
 *
 * Single toggle at the top binds a "safe default" mode. When on, every
 * setting below is locked to the TradingView-compatible value and shows
 * a lock icon. Tooltip on the toggle explains the implications of turning
 * it off. Pre-flight checks run live and emit blocking status to the
 * parent so the Run button can be disabled when needed.
 */
@Component({
  selector: 'app-tv-compat-panel',
  standalone: true,
  imports: [CommonModule, FormsModule, RouterModule],
  templateUrl: './tv-compat-panel.component.html',
  styleUrls: ['./tv-compat-panel.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class TvCompatPanelComponent {
  private readonly http = inject(HttpClient);

  // ----- Inputs from the parent Engine Lab -----
  readonly strategyName = input.required<string | null>();
  readonly symbol = input.required<string>();
  readonly timeframe = input<Timeframe>('15m');
  readonly startDate = input<string>('');
  readonly endDate = input<string>('');
  /** Indicators the current strategy uses. Parent knows the mapping. */
  readonly indicators = input<readonly IndicatorEntry[]>([]);

  // ----- Output to the parent: can the Run button proceed? -----
  readonly preflightStatus = output<PreflightResponse | null>();

  // ----- Locked settings -----
  // The master toggle. When `true`, all locked settings are forced to
  // their TV-compatible default and cannot be edited.
  readonly tvCompatible = signal(true);

  readonly sessionFilter = signal<SessionFilter>('rth_only');
  readonly priceAdjustment = signal<PriceAdjustment>('unadjusted');
  readonly warmupDays = signal<number>(90);
  readonly indicatorPeriodsCanonical = signal<boolean>(true);
  readonly barTimestamp = signal<BarTimestamp>('bar_open');
  readonly rsiSource = signal<'close' | 'hlc3'>('close');

  // ----- Pre-flight state -----
  readonly preflightLoading = signal(false);
  readonly preflightResult = signal<PreflightResponse | null>(null);
  readonly preflightError = signal<string | null>(null);

  // ----- Derived UI helpers -----
  readonly isLocked = computed(() => this.tvCompatible());
  readonly overallStatus = computed<Severity | 'unknown'>(() => {
    const r = this.preflightResult();
    return r ? r.overall : 'unknown';
  });
  readonly canRun = computed(() => {
    const r = this.preflightResult();
    return !r || r.overall !== 'blocking';
  });
  readonly researchLabTvTab = '/research-lab'; // tab 8 is TV vs Polygon Divergence

  constructor() {
    // When TV-compatible flips on, force every field to its safe default.
    effect(() => {
      if (this.tvCompatible()) {
        this.sessionFilter.set('rth_only');
        this.priceAdjustment.set('unadjusted');
        this.warmupDays.set(90);
        this.indicatorPeriodsCanonical.set(true);
        this.barTimestamp.set('bar_open');
        this.rsiSource.set('close');
      }
    });

    // Re-run pre-flight whenever any relevant field changes. Reads the
    // inputs + our local signals; Angular's effect tracking calls this
    // whenever anything used inside changes.
    effect(() => {
      const body = {
        strategy_name: this.strategyName() ?? 'unknown',
        symbol: this.symbol(),
        start_date: this.startDate() || '2025-01-01',
        end_date: this.endDate() || '2025-12-31',
        timeframe: this.timeframe(),
        indicators: this.indicators().map(i => ({ name: i.name, length: i.length })),
        session_filter: this.sessionFilter(),
        warmup_days: this.warmupDays(),
        dividend_adjustment: this.priceAdjustment() === 'split_and_dividend',
      };
      // Skip if no indicators listed yet (form not ready)
      if (body.indicators.length === 0) return;
      this.runPreflight(body);
    });
  }

  private async runPreflight(body: unknown): Promise<void> {
    this.preflightLoading.set(true);
    this.preflightError.set(null);
    try {
      const url = `${environment.pythonServiceUrl}/research/data-divergence/preflight`;
      const r = await firstValueFrom(this.http.post<PreflightResponse>(url, body));
      this.preflightResult.set(r);
      this.preflightStatus.emit(r);
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      this.preflightError.set(msg);
      this.preflightResult.set(null);
      this.preflightStatus.emit(null);
    } finally {
      this.preflightLoading.set(false);
    }
  }

  toggleTvCompatible(): void {
    this.tvCompatible.update(v => !v);
  }

  iconFor(status: Severity): string {
    return { ok: '✓', warning: '!', blocking: '✕' }[status];
  }
}
