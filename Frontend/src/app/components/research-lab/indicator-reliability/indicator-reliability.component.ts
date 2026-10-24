import {
  Component,
  signal,
  computed,
  inject,
  DestroyRef,
  ChangeDetectionStrategy,
  effect,
} from '@angular/core';
import { FormsModule } from '@angular/forms';
import { HttpClient } from '@angular/common/http';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of, finalize } from 'rxjs';
import { environment } from '../../../../environments/environment';
import { Select } from 'primeng/select';
import { InputNumber } from 'primeng/inputnumber';
import { MessageModule } from 'primeng/message';
import { ProgressSpinner } from 'primeng/progressspinner';
import { TooltipModule } from 'primeng/tooltip';
import { Checkbox } from 'primeng/checkbox';
import { MultiSelect } from 'primeng/multiselect';
import { TickerRangePickerComponent } from '../../../shared/ticker-range-picker/ticker-range-picker.component';
import type { TickerRange } from '../../../shared/ticker-range-picker/ticker-range-picker.types';
import { tickerRangeToWire } from '../../../utils/ticker-wire';
import { StudyPageComponent } from '../../../research/study-page/study-page.component';
import { createResultInputTracker } from '../../../shared/workspace/previous-result';
import type { IndicatorReliabilityResponse } from './indicator-reliability.models';
import { IndicatorReliabilityReportComponent } from './indicator-reliability-report/indicator-reliability-report.component';

// ─── Interfaces ────────────────────────────────────────────


interface IndicatorInfo {
  name: string;
  category: string;
  description: string;
  params: ParamConfig[];
}

interface ParamConfig {
  name: string;
  type: string;
  default: number;
  min?: number;
  max?: number;
  description: string;
}

interface IndicatorOption {
  label: string;
  value: string;
  category: string;
  description: string;
}

interface HorizonOption {
  label: string;
  value: number;
}

/** What a reliability request is made from: the inputs a shown result answers. */
interface ReliabilityStudyInputs {
  readonly range: TickerRange;
  readonly indicator: string;
  readonly params: Record<string, number>;
  readonly horizons: readonly number[];
  readonly includeSlope: boolean;
}

@Component({
  selector: 'app-indicator-reliability',
  templateUrl: './indicator-reliability.component.html',
  styleUrls: ['./indicator-reliability.component.scss'],
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    FormsModule,
    Select,
    InputNumber,
    MessageModule,
    ProgressSpinner,
    TooltipModule,
    Checkbox,
    MultiSelect,
    TickerRangePickerComponent,
    StudyPageComponent,
    IndicatorReliabilityReportComponent,
  ],
})
export class IndicatorReliabilityComponent {
  private http = inject(HttpClient);
  private destroyRef = inject(DestroyRef);
  private pythonUrl = environment.pythonServiceUrl;

  // Form inputs
  // Single TickerRange replaces the previous (ticker, fromDate, toDate)
  // signals — bound directly to <app-ticker-range-picker [(value)]="range">.
  // ``hideSampling=true`` collapses the Sampling card because indicator
  // reliability uses the indicator's own timeframe, not a per-form setting.
  range = signal<TickerRange>({
    symbol: 'AAPL',
    from: '2024-01-02',
    to: '2024-06-28',
    resolution: 'minute',
  });

  indicatorName = signal('rsi');
  includeSlope = signal(false);

  // Dynamic params (populated when indicator is selected)
  paramConfigs = signal<ParamConfig[]>([]);
  paramValues = signal<Record<string, number>>({});

  // Horizons multi-select
  allHorizons: HorizonOption[] = [
    { label: '1-bar', value: 1 },
    { label: '5-bar', value: 5 },
    { label: '10-bar', value: 10 },
    { label: '15-bar', value: 15 },
    { label: '30-bar', value: 30 },
  ];
  selectedHorizons = signal<number[]>([1, 5, 10, 15, 30]);

  // State
  loading = signal(false);
  loadingIndicators = signal(false);
  result = signal<IndicatorReliabilityResponse | null>(null);
  error = signal<string | null>(null);

  // Indicator options (loaded from API)
  indicatorOptions = signal<IndicatorOption[]>([]);
  groupedIndicators = signal<Record<string, IndicatorOption[]>>({});

  canRun = computed(() => {
    const r = this.range();
    return (
      r.symbol.trim().length > 0 &&
      this.indicatorName().trim().length > 0 &&
      r.from.trim().length > 0 &&
      r.to.trim().length > 0 &&
      this.selectedHorizons().length > 0 &&
      !this.loading()
    );
  });

  /** The inputs the shown result answers; a result is "previous" once these change. */
  private readonly enteredInputs = computed<ReliabilityStudyInputs>(() => ({
    range: this.range(),
    indicator: this.indicatorName(),
    params: this.paramValues(),
    horizons: this.selectedHorizons(),
    includeSlope: this.includeSlope(),
  }));
  readonly inputs = createResultInputTracker(
    this.enteredInputs,
    ({ range, indicator, params, horizons, includeSlope }) => ({
      key: JSON.stringify([
        range.symbol.toUpperCase(),
        range.from,
        range.to,
        indicator,
        params,
        [...horizons].sort((a, b) => a - b),
        includeSlope,
      ]),
      facts: [
        { label: 'Instrument', value: range.symbol.toUpperCase() },
        { label: 'Window', value: `${range.from} to ${range.to}` },
        { label: 'Indicator', value: `${indicator} ${JSON.stringify(params)}` },
        { label: 'Horizons', value: [...horizons].sort((a, b) => a - b).join(', ') + ' bars' },
        { label: 'Slope IC', value: includeSlope ? 'included' : 'not included' },
      ],
    }),
  );

  /** What step 3 says is about to run. */
  readonly readySummary = computed(() => {
    const r = this.range();
    return `${this.selectedIndicatorLabel} on ${r.symbol.toUpperCase()}, ${r.from} to ${r.to}`;
  });

  /** Why Run study cannot start, in words that point at the step to fix. */
  readonly blockedReason = computed<string | null>(() => {
    if (this.canRun() || this.loading()) return null;
    if (this.indicatorName().trim().length === 0) return 'Pick an indicator.';
    if (this.selectedHorizons().length === 0) return 'Pick at least one forward horizon.';
    return 'Choose an instrument and both dates.';
  });

  /** Anything past the idle state is on screen: the request, an error or a result. */
  readonly hasOutput = computed(
    () => this.loading() || this.error() !== null || this.result() !== null,
  );

  get selectedIndicatorLabel(): string {
    const found = this.indicatorOptions().find(i => i.value === this.indicatorName());
    return found ? found.label : this.indicatorName().toUpperCase();
  }

  constructor() {
    // Load indicators on init
    this.loadIndicators();

    // Load params when indicator changes
    effect(() => {
      const name = this.indicatorName();
      if (name) {
        this.loadIndicatorParams(name);
      }
    });
  }

  private loadIndicators(): void {
    this.loadingIndicators.set(true);
    this.http
      .get<Record<string, IndicatorInfo[]>>(`${this.pythonUrl}/api/research/indicators`)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          console.error('Failed to load indicators:', err);
          return of({});
        }),
        finalize(() => this.loadingIndicators.set(false)),
      )
      .subscribe(categories => {
        const options: IndicatorOption[] = [];
        const grouped: Record<string, IndicatorOption[]> = {};

        for (const [category, indicators] of Object.entries(categories)) {
          grouped[category] = [];
          for (const ind of indicators) {
            const opt: IndicatorOption = {
              label: ind.name.toUpperCase(),
              value: ind.name,
              category,
              description: ind.description,
            };
            options.push(opt);
            grouped[category].push(opt);
          }
        }

        this.indicatorOptions.set(options);
        this.groupedIndicators.set(grouped);
      });
  }

  private loadIndicatorParams(name: string): void {
    this.http
      .get<ParamConfig[]>(`${this.pythonUrl}/api/research/indicator-params/${name}`)
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(() => of([])),
      )
      .subscribe(params => {
        this.paramConfigs.set(params);
        // Set default values
        const defaults: Record<string, number> = {};
        for (const p of params) {
          defaults[p.name] = p.default;
        }
        this.paramValues.set(defaults);
      });
  }

  updateParam(name: string, value: number): void {
    this.paramValues.update(current => ({ ...current, [name]: value }));
  }

  /**
   * Run study. `loading` closes synchronously, so a second press before the
   * request settles is dropped and the request is sent once.
   */
  runAnalysis(): void {
    if (this.loading()) return;
    this.loading.set(true);
    this.error.set(null);
    this.result.set(null);
    this.inputs.clear();
    const submitted = this.enteredInputs();

    // Wire the canonical TickerRange shape into the request body via the
    // shared adapter. Python's IndicatorReliabilityRequest now inherits
    // TickerRequest (PR ii), so symbol/from_date/to_date are the canonical
    // fields. The legacy aliases (ticker/start_date/end_date) still work
    // until PR (iii)'s alias-removal commit, but we send canonical now.
    const wire = tickerRangeToWire({
      ...submitted.range,
      symbol: submitted.range.symbol.toUpperCase(),
    });
    const payload = {
      ...wire,
      indicator_name: submitted.indicator,
      indicator_params: submitted.params,
      horizons: submitted.horizons,
      include_slope: submitted.includeSlope,
    };

    this.http
      .post<IndicatorReliabilityResponse>(
        `${this.pythonUrl}/api/research/indicator-reliability`,
        payload,
      )
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          const msg = err?.error?.detail ?? err?.message ?? 'An unexpected error occurred';
          this.error.set(msg);
          return of(null);
        }),
        finalize(() => this.loading.set(false)),
      )
      .subscribe(res => {
        if (res) {
          this.result.set(res);
          if (!res.success && res.error) {
            this.error.set(res.error);
          } else if (res.success) {
            this.inputs.capture(submitted);
          }
        }
      });
  }

  /** Prompt + re-run with a new ticker (only wired CTA). */
  runOnAnotherTicker(): void {
    const current = this.range().symbol;
    const next = window.prompt('Run on another ticker (e.g. MSFT, GOOGL, NVDA):', current);
    if (!next) return;
    const cleaned = next.trim().toUpperCase();
    if (!cleaned || cleaned === current) return;
    this.range.set({ ...this.range(), symbol: cleaned });
    this.runAnalysis();
  }
}
