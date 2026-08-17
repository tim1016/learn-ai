import { HttpClient } from "@angular/common/http";
import { ChangeDetectionStrategy, Component, computed, inject, signal } from "@angular/core";
import { firstValueFrom } from "rxjs";

import { environment } from "../../../../../environments/environment";
import { JobsService } from "../../../../services/jobs.service";
import type { ParamProperty, StrategyInfo } from "../../../strategy-lab/strategy-lab.models";
import { RecencyParamRangeInputComponent } from "./recency-param-range-input.component";
import { computeGridSize, type ParamRange, type StrategyRangeConfig } from "./recency-param-range";

export type DurationPreset = "3m" | "6m" | "12m" | "24m" | "custom";

const PRESET_MONTHS: Record<Exclude<DurationPreset, "custom">, number> = { "3m": 3, "6m": 6, "12m": 12, "24m": 24 };
const MAX_MONTHS = 24;
const MS_PER_DAY = 24 * 60 * 60 * 1000;

function defaultRangeFor(prop: ParamProperty): ParamRange {
  const value = typeof prop.default === "number" ? prop.default : 0;
  return { type: "value_list", values: [value] };
}

/**
 * Recency Chart launch configuration surface (design spec D1, D4).
 * Symbols + eligible-strategy selection + per-strategy numeric param
 * ranges + duration preset, with a live pre-launch run-count estimate,
 * then dispatches the recency_chart job (Slice 1d).
 */
@Component({
  selector: "app-recency-launch-config",
  standalone: true,
  imports: [RecencyParamRangeInputComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-launch-config.component.html",
})
export class RecencyLaunchConfigComponent {
  private readonly http = inject(HttpClient);
  private readonly jobs = inject(JobsService);

  readonly allStrategies = signal<StrategyInfo[]>([]);
  readonly eligibleStrategies = computed(() => this.allStrategies().filter((s) => s.recency_supported === true));

  readonly symbolsText = signal("");
  readonly symbols = computed<string[]>(() =>
    this.symbolsText()
      .split(",")
      .map((s) => s.trim().toUpperCase())
      .filter((s) => s.length > 0),
  );

  readonly selectedStrategyKeys = signal<string[]>([]);
  readonly rangesByStrategy = signal<Record<string, Record<string, ParamRange>>>({});

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

  constructor() {
    void this.loadStrategies();
  }

  private async loadStrategies(): Promise<void> {
    const result = await firstValueFrom(
      this.http.get<StrategyInfo[]>(`${environment.pythonServiceUrl}/api/engine/strategies`),
    );
    this.allStrategies.set(result);
  }

  numericParams(strategy: StrategyInfo): [string, ParamProperty][] {
    const props: Record<string, ParamProperty> = strategy.params_schema.properties ?? {};
    return Object.entries(props).filter(
      ([name, prop]) => name !== "symbol" && (prop.type === "number" || prop.type === "integer"),
    );
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

  setSymbolsText(text: string): void {
    this.symbolsText.set(text);
  }

  setDurationPreset(preset: DurationPreset): void {
    this.durationPreset.set(preset);
  }

  setCustomMonths(raw: string): void {
    const parsed = Number(raw);
    const clamped = Number.isFinite(parsed) ? Math.min(MAX_MONTHS, Math.max(1, parsed)) : 1;
    this.customMonths.set(clamped);
  }

  async launch(): Promise<void> {
    const windowEndMs = Date.now();
    const windowStartMs = windowEndMs - this.windowMonths() * 30 * MS_PER_DAY;
    await this.jobs.startJob("recency_chart", {
      strategies: this.strategyConfigs().map((s) => ({ strategyKey: s.strategyKey, paramRanges: s.paramRanges })),
      symbols: this.symbols(),
      windowStartMs,
      windowEndMs,
    });
  }
}
