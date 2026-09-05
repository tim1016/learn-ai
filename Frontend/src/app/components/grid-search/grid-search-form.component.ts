import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, output, signal, untracked } from '@angular/core';
import { DecimalPipe } from '@angular/common';
import { ButtonModule } from 'primeng/button';
import { InputText } from 'primeng/inputtext';

import { etDayEndMs, etIsoDate, etMidnightMs } from '../../shared/date/et-midnight';
import { ParamRangeInputComponent } from '../../shared/param-range/param-range-input.component';
import { defaultNumericValue, numericStrategyParams, rangeVaries, type ParamRange } from '../../shared/param-range/param-range';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import { TimestampDisplayComponent } from '../../shared/timestamp';
import type { ParamProperty, StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { GridSearchRefusedError, GridSearchService } from './grid-search.service';
import {
  RANKING_MEASURES,
  type GridSearchPreflight,
  type GridSearchRefusal,
  type GridSearchSpecRequest,
  type RankingMeasure,
} from './grid-search.types';

const MS_PER_DAY = 24 * 60 * 60 * 1000;
const DEFAULT_WINDOW_DAYS = 2 * 365;

export interface GridSearchLaunch {
  readonly jobId: string;
}

interface ParamRow {
  readonly name: string;
  readonly property: ParamProperty;
  readonly vary: boolean;
  readonly range: ParamRange;
  readonly fixedValue: number;
}

function selectValue(event: Event): string | null {
  return event.target instanceof HTMLSelectElement ? event.target.value : null;
}

function defaultWindow(now: number): { from: string; to: string } {
  return { from: etIsoDate(now - DEFAULT_WINDOW_DAYS * MS_PER_DAY), to: etIsoDate(now - MS_PER_DAY) };
}

/**
 * Grid Search setup (PRD #1926 "Frontend"): symbol, strategy from the sweepable
 * catalogue with ineligible entries explained, the window, costs, per-parameter
 * vary toggles with the shared range editor, and a live server preflight —
 * count, limit, labelled estimate, run-up plan — before launch.
 */
@Component({
  selector: 'app-grid-search-form',
  imports: [ButtonModule, InputText, DecimalPipe, ParamRangeInputComponent, ReceiptLabelPipe, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './grid-search-form.component.html',
  styleUrl: './grid-search-form.component.scss',
})
export class GridSearchFormComponent {
  private readonly service = inject(GridSearchService);
  private readonly destroyRef = inject(DestroyRef);

  readonly strategies = input.required<readonly StrategyInfo[]>();
  /** Debounce before the form asks the server to preflight; tests set 0. */
  readonly preflightDebounceMs = input(300);
  /**
   * Embedded inside another form (Walk-Forward): no header, no footer, no
   * preflight of its own — every edit emits the spec through `specChanged`
   * and the host decides what to ask the server.
   */
  readonly embedded = input(false);
  /** A spec to start from (a completed grid search's request); applied once its strategy is known. */
  readonly initial = input<GridSearchSpecRequest | null>(null);
  readonly launched = output<GridSearchLaunch>();
  readonly specChanged = output<GridSearchSpecRequest | null>();

  readonly symbol = signal('SPY');
  readonly strategyKey = signal<string | null>(null);
  readonly fromDate = signal(defaultWindow(Date.now()).from);
  readonly toDate = signal(defaultWindow(Date.now()).to);
  readonly fillMode = signal('signal_bar_close');
  readonly resolution = signal<'minute' | 'daily'>('minute');
  readonly commissionPerOrder = signal(1);
  readonly slippagePerShare = signal(0);
  readonly initialCash = signal(100_000);
  readonly measure = signal<RankingMeasure>('sharpe_ratio');
  readonly minTrades = signal(5);
  readonly params = signal<readonly ParamRow[]>([]);

  readonly preflight = signal<GridSearchPreflight | null>(null);
  readonly refusal = signal<GridSearchRefusal | null>(null);
  readonly preflightError = signal<string | null>(null);
  readonly checking = signal(false);
  readonly launching = signal(false);

  protected readonly measures = RANKING_MEASURES;
  protected readonly eligible = computed(() => this.strategies().filter((s) => s.sweep_eligibility?.eligible === true));
  protected readonly ineligible = computed(() => this.strategies().filter((s) => s.sweep_eligibility?.eligible !== true));
  protected readonly resolutions = computed(() => this.selectedStrategy()?.supported_resolutions ?? ['minute']);
  protected readonly selectedStrategy = computed(() => this.strategies().find((s) => s.name === this.strategyKey()) ?? null);
  protected readonly variedCount = computed(() => this.params().filter((p) => p.vary).length);
  protected readonly canLaunch = computed(() => this.preflight() !== null && this.refusal() === null && !this.launching() && !this.checking());

  private debounce: ReturnType<typeof setTimeout> | null = null;
  /** Generation of the latest edit; a preflight that returns for an older generation is ignored. */
  private preflightGeneration = 0;
  private appliedInitial: GridSearchSpecRequest | null = null;

  constructor() {
    effect(() => {
      const strategies = this.eligible();
      const initial = this.initial();
      if (initial !== null && initial !== this.appliedInitial && strategies.some((s) => s.name === initial.strategy_key)) {
        this.appliedInitial = initial;
        untracked(() => this.applyInitial(initial));
        return;
      }
      if (this.strategyKey() === null && strategies.length > 0) untracked(() => this.selectStrategy(strategies[0].name));
    });
    this.destroyRef.onDestroy(() => {
      if (this.debounce !== null) clearTimeout(this.debounce);
    });
  }

  /** The exact wire body the server preflights and launches. */
  spec(): GridSearchSpecRequest | null {
    const strategy = this.strategyKey();
    if (strategy === null) return null;
    const param_ranges: Record<string, ParamRange> = {};
    for (const row of this.params()) {
      param_ranges[row.name] = row.vary ? row.range : { type: 'value_list', values: [row.fixedValue] };
    }
    return {
      strategy_key: strategy,
      symbol: this.symbol().trim().toUpperCase(),
      param_ranges,
      start_ms: etMidnightMs(this.fromDate()),
      end_ms: etDayEndMs(this.toDate()),
      resolution: this.resolution(),
      fill_mode: this.fillMode(),
      commission_per_order: this.commissionPerOrder(),
      slippage_per_share: this.slippagePerShare(),
      initial_cash: this.initialCash(),
      measure: this.measure(),
      min_trades: this.minTrades(),
    };
  }

  selectStrategy(name: string): void {
    this.strategyKey.set(name);
    const strategy = this.strategies().find((s) => s.name === name);
    if (strategy && !strategy.supported_resolutions.includes(this.resolution())) {
      this.resolution.set(strategy.supported_resolutions.includes('daily') ? 'daily' : 'minute');
    }
    this.params.set(
      strategy
        ? numericStrategyParams(strategy).map(([paramName, property]) => ({
            name: paramName,
            property,
            vary: false,
            range: { type: 'value_list', values: [defaultNumericValue(property)] },
            fixedValue: defaultNumericValue(property),
          }))
        : [],
    );
    this.scheduleRefresh();
  }

  onStrategyEvent(event: Event): void {
    if (event.target instanceof HTMLSelectElement) this.selectStrategy(event.target.value);
  }

  setSymbol(raw: string): void {
    this.symbol.set(raw);
    this.scheduleRefresh();
  }

  setDate(which: 'from' | 'to', raw: string): void {
    (which === 'from' ? this.fromDate : this.toDate).set(raw);
    this.scheduleRefresh();
  }

  setNumber(field: 'commission' | 'slippage' | 'cash' | 'minTrades', raw: string): void {
    const value = Number(raw);
    if (!Number.isFinite(value)) return;
    ({ commission: this.commissionPerOrder, slippage: this.slippagePerShare, cash: this.initialCash, minTrades: this.minTrades })[field].set(value);
    this.scheduleRefresh();
  }

  onMeasureEvent(event: Event): void {
    const raw = selectValue(event);
    if (raw !== null && (RANKING_MEASURES as readonly string[]).includes(raw)) {
      this.measure.set(raw as RankingMeasure);
      this.scheduleRefresh();
    }
  }

  onFillModeEvent(event: Event): void {
    const raw = selectValue(event);
    if (raw !== null) {
      this.fillMode.set(raw);
      this.scheduleRefresh();
    }
  }

  onResolutionEvent(event: Event): void {
    const raw = selectValue(event);
    if (raw === 'minute' || raw === 'daily') {
      this.resolution.set(raw);
      this.scheduleRefresh();
    }
  }

  onVaryEvent(name: string, event: Event): void {
    if (event.target instanceof HTMLInputElement) this.toggleVary(name, event.target.checked);
  }

  toggleVary(name: string, vary: boolean): void {
    this.params.update((rows) => rows.map((row) => (row.name === name ? { ...row, vary } : row)));
    this.scheduleRefresh();
  }

  setFixed(name: string, raw: string): void {
    const value = Number(raw);
    if (!Number.isFinite(value)) return;
    this.params.update((rows) => rows.map((row) => (row.name === name ? { ...row, fixedValue: value } : row)));
    this.scheduleRefresh();
  }

  setRange(name: string, range: ParamRange): void {
    this.params.update((rows) => rows.map((row) => (row.name === name ? { ...row, range } : row)));
    this.scheduleRefresh();
  }

  scheduleRefresh(): void {
    if (this.debounce !== null) clearTimeout(this.debounce);
    // An edit invalidates whatever preflight is showing or in flight; Launch waits for the next answer.
    this.preflightGeneration += 1;
    this.preflight.set(null);
    this.debounce = setTimeout(() => {
      if (this.embedded()) this.specChanged.emit(this.safeSpec());
      else void this.refreshPreflight();
    }, this.preflightDebounceMs());
  }

  /** Seed every control from a stored request, then let the host (or the preflight) see it once. */
  private applyInitial(initial: GridSearchSpecRequest): void {
    this.symbol.set(initial.symbol);
    this.fromDate.set(etIsoDate(initial.start_ms));
    this.toDate.set(etIsoDate(initial.end_ms - 1));
    this.fillMode.set(initial.fill_mode);
    this.resolution.set(initial.resolution);
    this.commissionPerOrder.set(initial.commission_per_order);
    this.slippagePerShare.set(initial.slippage_per_share);
    this.initialCash.set(initial.initial_cash);
    this.measure.set(initial.measure);
    this.minTrades.set(initial.min_trades);
    this.selectStrategy(initial.strategy_key);
    this.params.update((rows) =>
      rows.map((row) => {
        const range = initial.param_ranges[row.name];
        if (range === undefined) return row;
        const vary = rangeVaries(range);
        return { ...row, vary, range, fixedValue: range.type === 'value_list' ? range.values[0] : row.fixedValue };
      }),
    );
    this.scheduleRefresh();
  }

  async refreshPreflight(): Promise<void> {
    const spec = this.safeSpec();
    if (spec === null) return;
    const generation = ++this.preflightGeneration;
    this.checking.set(true);
    this.preflight.set(null);
    try {
      const plan = await this.service.preflight(spec);
      if (generation !== this.preflightGeneration) return;
      this.preflight.set(plan);
      this.refusal.set(null);
      this.preflightError.set(null);
    } catch (error) {
      if (generation !== this.preflightGeneration) return;
      if (error instanceof GridSearchRefusedError) {
        this.refusal.set(error.refusal);
        this.preflightError.set(null);
      } else {
        this.refusal.set(null);
        this.preflightError.set('The preflight could not be completed. Check the service and try again.');
      }
    } finally {
      if (generation === this.preflightGeneration) this.checking.set(false);
    }
  }

  async launch(): Promise<void> {
    const spec = this.safeSpec();
    if (spec === null || !this.canLaunch()) return;
    this.launching.set(true);
    try {
      const jobId = await this.service.launch(spec);
      this.launched.emit({ jobId });
    } catch (error) {
      if (error instanceof GridSearchRefusedError) this.refusal.set(error.refusal);
      else this.preflightError.set('The launch was not accepted. Check the service and try again.');
    } finally {
      this.launching.set(false);
    }
  }

  protected runUpSentence(plan: GridSearchPreflight): string {
    const sessions = plan.run_up.run_up_sessions;
    const days = `${sessions} trading day${sessions === 1 ? '' : 's'}`;
    return plan.run_up.carved_from_range
      ? `Slowest setting needs ${plan.run_up.required_samples} bars → run-up uses the first ${days} of your range.`
      : `Slowest setting needs ${plan.run_up.required_samples} bars → primed from ${days} the lake already holds before your start.`;
  }

  private safeSpec(): GridSearchSpecRequest | null {
    try {
      return this.spec();
    } catch {
      this.preflightError.set('Enter both dates as YYYY-MM-DD.');
      return null;
    }
  }
}
