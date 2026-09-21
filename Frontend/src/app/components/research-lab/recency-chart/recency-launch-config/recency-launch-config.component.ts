import { HttpClient } from "@angular/common/http";
import { ChangeDetectionStrategy, Component, computed, effect, inject, output, signal, untracked } from "@angular/core";
import { ButtonModule } from "primeng/button";
import { firstValueFrom } from "rxjs";

import { environment } from "../../../../../environments/environment";
import { JobsService } from "../../../../services/jobs.service";
import type { StrategyInfo } from "../../../strategy-lab/strategy-lab.models";
import { DEFAULT_ADJUSTMENT_MODE } from "../../../../shared/ticker-catalog";
import { SymbolCatalogService } from "../../../../shared/symbol-catalog/symbol-catalog.service";
import { MultiInstrumentCardComponent } from "../../../../shared/multi-ticker-range-picker/multi-instrument-card.component";
import { RecencyDurationInputComponent, type DurationPreset } from "./recency-duration-input.component";
import { RecencyStrategySelectionComponent } from "./recency-strategy-selection.component";
import { computeGridSize, defaultRangeForParameter, numericStrategyParams, type ParamRange, type StrategyRangeConfig, rangeProblem } from "../../../../shared/param-range/param-range";

const PRESET_MONTHS: Record<Exclude<DurationPreset, "custom">, number> = { "3m": 3, "6m": 6, "12m": 12, "24m": 24 };
const MAX_MONTHS = 24;
const MS_PER_DAY = 24 * 60 * 60 * 1000;
const DEFAULT_SYMBOLS: readonly string[] = ["SPY"];

/**
 * Recency Chart launch configuration surface (design spec D1, D4).
 * Symbols + eligible-strategy selection + per-strategy numeric param
 * ranges + duration preset, with a live pre-launch run-count estimate,
 * then dispatches the recency_chart job (Slice 1d).
 */
@Component({
  selector: "app-recency-launch-config",
  imports: [
    ButtonModule,
    MultiInstrumentCardComponent,
    RecencyDurationInputComponent,
    RecencyStrategySelectionComponent,
  ],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-launch-config.component.html",
  styleUrl: "./recency-launch-config.component.scss",
})
export class RecencyLaunchConfigComponent {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);

  readonly allStrategies = signal<StrategyInfo[]>([]);
  readonly eligibleStrategies = computed(() => this.allStrategies().filter((s) => s.recency_supported === true));

  /**
   * The timeline's universe, picked from the shared multi card over the
   * joined catalog: every listed symbol offered, unheld picks gated on
   * their backfill (the timelines read the lake's split-adjusted minute
   * bars, so an ungated pick would strand lanes with no data).
   */
  readonly symbols = signal<string[]>([...DEFAULT_SYMBOLS]);
  /** The tree the recency job reads — its data policy is split-adjusted minute bars. */
  readonly pickerAdjustmentMode = DEFAULT_ADJUSTMENT_MODE;
  private readonly catalog = inject(SymbolCatalogService);
  protected readonly catalogView = computed(() =>
    // `viewFor` installs a resource — step outside tracking (NG0602).
    untracked(() => this.catalog.viewFor(DEFAULT_ADJUSTMENT_MODE)),
  );
  readonly attemptedLaunch = signal(false);
  readonly customMonthsError = signal<string | null>(null);
  readonly strategyValidationMessage = computed(() =>
    this.attemptedLaunch() && this.strategyConfigs().length === 0
      ? "Select at least one strategy before launching the timeline."
      : null,
  );
  readonly symbolValidationMessage = computed(() =>
    this.attemptedLaunch() && this.symbols().length === 0 ? "Add at least one symbol before launching the timeline." : null,
  );
  /** The first selected parameter whose range the editor is refusing (#1940), named for the operator. */
  private readonly refusedParameter = computed(() => {
    for (const strategy of this.strategyConfigs()) {
      const refused = Object.entries(strategy.paramRanges).find(([, range]) => rangeProblem(range) !== null);
      if (refused === undefined) continue;
      const [paramName] = refused;
      const info = this.allStrategies().find((s) => s.name === strategy.strategyKey);
      const title = info?.params_schema.properties?.[paramName]?.title;
      return `${info?.display_name ?? strategy.strategyKey} · ${title ?? paramName}`;
    }
    return null;
  });
  readonly rangeValidationMessage = computed(() => {
    const refused = this.refusedParameter();
    return this.attemptedLaunch() && refused !== null ? `Fix the values for ${refused} before launching the timeline.` : null;
  });

  readonly selectedStrategyKeys = signal<string[]>([]);
  readonly rangesByStrategy = signal<Record<string, Record<string, ParamRange>>>({});
  readonly selectedStrategies = computed(() => {
    const selectedKeys = new Set(this.selectedStrategyKeys());
    return this.eligibleStrategies().filter((strategy) => selectedKeys.has(strategy.name));
  });

  readonly durationPreset = signal<DurationPreset>("6m");
  readonly customMonths = signal<number>(6);

  readonly windowMonths = computed(() => {
    const preset = this.durationPreset();
    return preset === "custom" ? this.customMonths() : PRESET_MONTHS[preset];
  });

  readonly strategyConfigs = computed<StrategyRangeConfig[]>(() =>
    this.selectedStrategyKeys().map((key) => ({
      strategyKey: key,
      paramRanges: this.rangesByStrategy()[key] ?? {},
    })),
  );

  readonly runCount = computed(() => computeGridSize(this.strategyConfigs(), this.symbols()));

  readonly runCountLabel = computed(() => {
    const count = this.runCount();
    return `${count} ${count === 1 ? "run" : "runs"}`;
  });

  /** Fires once when the most recently launched job's status turns
   * 'completed', so the chart page can reload its projections — without
   * this, a launch's persisted trades stay invisible until a manual
   * page reload since startJob only resolves once Python accepts the
   * job (202), long before persistence finishes. */
  readonly launchCompleted = output();
  private readonly launchedJobId = signal<string | null>(null);
  private notifiedForJobId: string | null = null;

  constructor() {
    void this.loadStrategies();
    effect(() => {
      const jobId = this.launchedJobId();
      if (!jobId || this.notifiedForJobId === jobId) return;
      if (this.jobs.job(jobId)?.status === "completed") {
        this.notifiedForJobId = jobId;
        this.launchCompleted.emit();
      }
    });
  }

  private async loadStrategies(): Promise<void> {
    const result = await firstValueFrom(
      this.http.get<StrategyInfo[]>(`${environment.pythonServiceUrl}/api/engine/strategies`),
    );
    this.allStrategies.set(result);
    const defaultStrategy = this.selectedStrategyKeys().length === 0
      ? result.find((strategy) => strategy.recency_supported === true)
      : undefined;
    if (defaultStrategy) this.selectStrategy(defaultStrategy);
  }

  toggleStrategy(strategy: StrategyInfo): void {
    const keys = this.selectedStrategyKeys();
    if (keys.includes(strategy.name)) {
      this.selectedStrategyKeys.set(keys.filter((k) => k !== strategy.name));
      return;
    }

    this.selectStrategy(strategy);
  }

  private selectStrategy(strategy: StrategyInfo): void {
    const keys = this.selectedStrategyKeys();
    if (keys.includes(strategy.name)) return;

    this.selectedStrategyKeys.set([...keys, strategy.name]);
    const defaults: Record<string, ParamRange> = {};
    for (const [name, prop] of numericStrategyParams(strategy)) {
      defaults[name] = defaultRangeForParameter(prop);
    }
    this.rangesByStrategy.update((m) => ({ ...m, [strategy.name]: defaults }));
  }

  rangeFor(strategyKey: string, paramName: string): ParamRange {
    return this.rangesByStrategy()[strategyKey]?.[paramName] ?? { type: "value_list", values: [0] };
  }

  updateRange(strategyKey: string, paramName: string, range: ParamRange): void {
    this.rangesByStrategy.update((m) => ({
      ...m,
      [strategyKey]: { ...m[strategyKey], [paramName]: range },
    }));
  }

  retryCoverage(): void {
    this.catalogView().reload();
  }

  retryVendorCatalog(): void {
    this.catalogView().retryVendor();
  }

  setDurationPreset(preset: DurationPreset): void {
    this.durationPreset.set(preset);
  }

  /**
   * The control means whole months, so a fraction is refused rather than
   * silently kept: 1.5 previously survived clamping and produced a 45-day
   * window from a field labelled "months".
   */
  setCustomMonths(raw: string): void {
    const trimmed = raw.trim();
    const parsed = Number(trimmed);
    if (trimmed === "" || !Number.isFinite(parsed) || !Number.isInteger(parsed)) {
      this.customMonthsError.set("Enter a whole number of months.");
      return;
    }
    // Out-of-range still clamps -- that is the established behaviour of this
    // number control. Only a non-whole value is refused outright, because
    // clamping cannot express "months are whole" without inventing a value.
    this.customMonthsError.set(null);
    this.customMonths.set(Math.min(MAX_MONTHS, Math.max(1, parsed)));
  }

  async launch(): Promise<void> {
    this.attemptedLaunch.set(true);
    if (this.symbols().length === 0) return;
    // Deselecting the last strategy previously launched a job with an empty
    // strategy list and no local error.
    if (this.strategyConfigs().length === 0) return;
    if (this.customMonthsError() !== null) return;
    if (this.rangeValidationMessage() !== null) return;

    const windowEndMs = Date.now();
    const windowStartMs = windowEndMs - this.windowMonths() * 30 * MS_PER_DAY;
    const jobId = await this.jobs.startJob("recency_chart", {
      strategies: this.strategyConfigs().map((s) => ({ strategyKey: s.strategyKey, paramRanges: s.paramRanges })),
      symbols: this.symbols(),
      windowStartMs,
      windowEndMs,
    });
    this.launchedJobId.set(jobId);
  }
}
