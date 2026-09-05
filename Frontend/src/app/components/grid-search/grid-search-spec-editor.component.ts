import { ChangeDetectionStrategy, Component, computed, DestroyRef, effect, inject, input, output, signal, untracked } from '@angular/core';
import { InputText } from 'primeng/inputtext';

import { etDayEndMs, etIsoDate, etMidnightMs } from '../../shared/date/et-midnight';
import { ParamRangeInputComponent } from '../../shared/param-range/param-range-input.component';
import { defaultNumericValue, numericStrategyParams, rangeVaries, type ParamRange } from '../../shared/param-range/param-range';
import { ReceiptLabelPipe } from '../../shared/pipes/receipt-label.pipe';
import type { ParamProperty, StrategyInfo } from '../strategy-lab/strategy-lab.models';
import { RANKING_MEASURES, type GridSearchSpecRequest, type RankingMeasure } from './grid-search.types';

const MS_PER_DAY = 24 * 60 * 60 * 1000;
const DEFAULT_WINDOW_DAYS = 2 * 365;

/** What the editor reports after every edit: the wire spec, or why one cannot be built yet. */
export interface GridSpecEdit {
  readonly spec: GridSearchSpecRequest | null;
  readonly problem: string | null;
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
 * The grid-search spec editor (PRD #1926 "Frontend"), shared by Grid Search
 * and Walk-Forward: symbol, strategy from the sweepable catalogue with
 * ineligible entries explained, the window, costs and ranking, and
 * per-parameter vary toggles with the shared range editor. It owns no
 * preflight and no service: every (debounced) edit is emitted as
 * `specChanged` and the host decides what to ask the server.
 */
@Component({
  selector: 'app-grid-search-spec-editor',
  imports: [InputText, ParamRangeInputComponent, ReceiptLabelPipe],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './grid-search-spec-editor.component.html',
  styleUrl: './grid-search-spec-editor.component.scss',
})
export class GridSearchSpecEditorComponent {
  private readonly destroyRef = inject(DestroyRef);

  readonly strategies = input.required<readonly StrategyInfo[]>();
  /** A spec to start from (a stored request); applied once its strategy is in the catalogue. */
  readonly initial = input<GridSearchSpecRequest | null>(null);
  /** Debounce between an edit and its `specChanged`; tests set 0. */
  readonly debounceMs = input(300);
  readonly specChanged = output<GridSpecEdit>();

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

  protected readonly measures = RANKING_MEASURES;
  protected readonly eligible = computed(() => this.strategies().filter((s) => s.sweep_eligibility?.eligible === true));
  protected readonly ineligible = computed(() => this.strategies().filter((s) => s.sweep_eligibility?.eligible !== true));
  protected readonly resolutions = computed(() => this.selectedStrategy()?.supported_resolutions ?? ['minute']);
  protected readonly selectedStrategy = computed(() => this.strategies().find((s) => s.name === this.strategyKey()) ?? null);
  protected readonly variedCount = computed(() => this.params().filter((p) => p.vary).length);

  private debounce: ReturnType<typeof setTimeout> | null = null;
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

  /** The exact wire body the server preflights and launches; throws on an unparseable date. */
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
    this.scheduleEmit();
  }

  onStrategyEvent(event: Event): void {
    if (event.target instanceof HTMLSelectElement) this.selectStrategy(event.target.value);
  }

  setSymbol(raw: string): void {
    this.symbol.set(raw);
    this.scheduleEmit();
  }

  setDate(which: 'from' | 'to', raw: string): void {
    (which === 'from' ? this.fromDate : this.toDate).set(raw);
    this.scheduleEmit();
  }

  setNumber(field: 'commission' | 'slippage' | 'cash' | 'minTrades', raw: string): void {
    const value = Number(raw);
    if (!Number.isFinite(value)) return;
    ({ commission: this.commissionPerOrder, slippage: this.slippagePerShare, cash: this.initialCash, minTrades: this.minTrades })[field].set(value);
    this.scheduleEmit();
  }

  onMeasureEvent(event: Event): void {
    const raw = selectValue(event);
    if (raw !== null && (RANKING_MEASURES as readonly string[]).includes(raw)) {
      this.measure.set(raw as RankingMeasure);
      this.scheduleEmit();
    }
  }

  onFillModeEvent(event: Event): void {
    const raw = selectValue(event);
    if (raw !== null) {
      this.fillMode.set(raw);
      this.scheduleEmit();
    }
  }

  onResolutionEvent(event: Event): void {
    const raw = selectValue(event);
    if (raw === 'minute' || raw === 'daily') {
      this.resolution.set(raw);
      this.scheduleEmit();
    }
  }

  onVaryEvent(name: string, event: Event): void {
    if (event.target instanceof HTMLInputElement) this.toggleVary(name, event.target.checked);
  }

  toggleVary(name: string, vary: boolean): void {
    this.params.update((rows) => rows.map((row) => (row.name === name ? { ...row, vary } : row)));
    this.scheduleEmit();
  }

  setFixed(name: string, raw: string): void {
    const value = Number(raw);
    if (!Number.isFinite(value)) return;
    this.params.update((rows) => rows.map((row) => (row.name === name ? { ...row, fixedValue: value } : row)));
    this.scheduleEmit();
  }

  setRange(name: string, range: ParamRange): void {
    this.params.update((rows) => rows.map((row) => (row.name === name ? { ...row, range } : row)));
    this.scheduleEmit();
  }

  private scheduleEmit(): void {
    if (this.debounce !== null) clearTimeout(this.debounce);
    this.debounce = setTimeout(() => this.specChanged.emit(this.edit()), this.debounceMs());
  }

  private edit(): GridSpecEdit {
    try {
      return { spec: this.spec(), problem: null };
    } catch {
      return { spec: null, problem: 'Enter both dates as YYYY-MM-DD.' };
    }
  }

  /** Seed every control from a stored request, then report it once like any other edit. */
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
        return { ...row, vary: rangeVaries(range), range, fixedValue: range.type === 'value_list' ? range.values[0] : row.fixedValue };
      }),
    );
    this.scheduleEmit();
  }
}
