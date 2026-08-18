import { HttpClient } from "@angular/common/http";
import { ChangeDetectionStrategy, Component, computed, effect, inject, output, signal } from "@angular/core";
import { firstValueFrom } from "rxjs";

import { environment } from "../../../../../environments/environment";
import { JobsService } from "../../../../services/jobs.service";
import type { ParamProperty, StrategyInfo } from "../../../strategy-lab/strategy-lab.models";
import { AssetIdentityComponent } from "../../../../shared/asset-identity";
import { RecencyParamRangeInputComponent } from "./recency-param-range-input.component";
import { computeGridSize, type ParamRange, type StrategyRangeConfig } from "./recency-param-range";

export type DurationPreset = "3m" | "6m" | "12m" | "24m" | "custom";

const PRESET_MONTHS: Record<Exclude<DurationPreset, "custom">, number> = { "3m": 3, "6m": 6, "12m": 12, "24m": 24 };
const DURATION_OPTIONS: readonly { readonly preset: DurationPreset; readonly label: string }[] = [
  { preset: "3m", label: "3 months" },
  { preset: "6m", label: "6 months" },
  { preset: "12m", label: "12 months" },
  { preset: "24m", label: "24 months" },
  { preset: "custom", label: "Custom" },
];
const MAX_MONTHS = 24;
const MS_PER_DAY = 24 * 60 * 60 * 1000;
const DEFAULT_SYMBOLS: readonly string[] = ["SPY"];

function numericDefault(prop: ParamProperty): number {
  return typeof prop.default === "number" ? prop.default : 0;
}

function defaultRangeFor(prop: ParamProperty): ParamRange {
  return { type: "value_list", values: [numericDefault(prop)] };
}

function parseSymbols(raw: string): string[] {
  return raw
    .split(",")
    .map((symbol) => symbol.trim().toUpperCase())
    .filter((symbol) => symbol.length > 0);
}

function uniqueSymbols(symbols: readonly string[]): string[] {
  return Array.from(new Set(symbols));
}

/**
 * Recency Chart launch configuration surface (design spec D1, D4).
 * Symbols + eligible-strategy selection + per-strategy numeric param
 * ranges + duration preset, with a live pre-launch run-count estimate,
 * then dispatches the recency_chart job (Slice 1d).
 */
@Component({
  selector: "app-recency-launch-config",
  imports: [AssetIdentityComponent, RecencyParamRangeInputComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-launch-config.component.html",
  styleUrl: "./recency-launch-config.component.scss",
})
export class RecencyLaunchConfigComponent {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);

  readonly allStrategies = signal<StrategyInfo[]>([]);
  readonly eligibleStrategies = computed(() => this.allStrategies().filter((s) => s.recency_supported === true));

  readonly symbolDraft = signal("");
  readonly committedSymbols = signal<string[]>([...DEFAULT_SYMBOLS]);
  readonly symbols = computed<string[]>(() => uniqueSymbols([...this.committedSymbols(), ...parseSymbols(this.symbolDraft())]));
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

  readonly selectedStrategyKeys = signal<string[]>([]);
  readonly rangesByStrategy = signal<Record<string, Record<string, ParamRange>>>({});
  readonly selectedStrategies = computed(() => {
    const selectedKeys = new Set(this.selectedStrategyKeys());
    return this.eligibleStrategies().filter((strategy) => selectedKeys.has(strategy.name));
  });

  readonly durationPreset = signal<DurationPreset>("6m");
  readonly customMonths = signal<number>(6);
  readonly durationOptions = DURATION_OPTIONS;

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

  numericParams(strategy: StrategyInfo): [string, ParamProperty][] {
    const props: Record<string, ParamProperty> = strategy.params_schema.properties ?? {};
    return Object.entries(props).filter(
      ([name, prop]) => name !== "symbol" && (prop.type === "number" || prop.type === "integer"),
    );
  }

  defaultNumericValue(prop: ParamProperty): number {
    return numericDefault(prop);
  }

  isSelected(strategy: StrategyInfo): boolean {
    return this.selectedStrategyKeys().includes(strategy.name);
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
    for (const [name, prop] of this.numericParams(strategy)) {
      defaults[name] = defaultRangeFor(prop);
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

  onSymbolsInput(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.symbolDraft.set(event.target.value);
    }
  }

  onSymbolsKeydown(event: KeyboardEvent): void {
    if (event.key === "Enter" || event.key === ",") {
      event.preventDefault();
      this.commitSymbolDraft();
    }
  }

  commitSymbolDraft(): void {
    const additions = parseSymbols(this.symbolDraft());
    if (additions.length === 0) return;
    this.committedSymbols.update((symbols) => uniqueSymbols([...symbols, ...additions]));
    this.symbolDraft.set("");
  }

  removeSymbol(symbol: string): void {
    this.committedSymbols.update((symbols) => symbols.filter((candidate) => candidate !== symbol));
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

  onCustomMonthsInput(event: Event): void {
    if (event.target instanceof HTMLInputElement) {
      this.setCustomMonths(event.target.value);
    }
  }

  async launch(): Promise<void> {
    this.attemptedLaunch.set(true);
    this.commitSymbolDraft();
    if (this.symbols().length === 0) return;
    // Deselecting the last strategy previously launched a job with an empty
    // strategy list and no local error.
    if (this.strategyConfigs().length === 0) return;
    if (this.customMonthsError() !== null) return;

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
