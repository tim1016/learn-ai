import { ChangeDetectionStrategy, Component, computed, input, model, output } from "@angular/core";

import type { DataPolicy } from "../../../models/data-policy";
import { InstrumentCardComponent } from "../../../shared/ticker-range-picker/parts/instrument-card.component";
import { TimeWindowCardComponent } from "../../../shared/ticker-range-picker/parts/time-window-card.component";
import type { TickerOption, TickerRange } from "../../../shared/ticker-range-picker/ticker-range-picker.types";
import type { EngineChoice, StrategyInfo } from "../strategy-lab.models";

export interface StrategyParameterChange {
  field: string;
  rawValue: string;
  type: string | undefined;
}

interface StrategyLabPrimaryAction {
  kind: "check-launcher" | "run";
  label: string;
  disabled: boolean;
}

@Component({
  selector: "app-strategy-lab-config-rail",
  imports: [InstrumentCardComponent, TimeWindowCardComponent],
  templateUrl: "./strategy-lab-config-rail.component.html",
  styleUrl: "./strategy-lab-config-rail.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class StrategyLabConfigRailComponent {
  readonly engineChoices: readonly EngineChoice[] = ["python", "lean", "both"];

  readonly collapsed = input(false);
  readonly engine = input.required<EngineChoice>();
  readonly range = model.required<TickerRange>();
  readonly dataPolicy = input.required<DataPolicy>();
  readonly strategies = input<readonly StrategyInfo[]>([]);
  readonly selectedStrategyName = input<string | null>(null);
  readonly paramValues = input<Record<string, unknown>>({});
  readonly fillMode = input<"signal_bar_close" | "next_bar_open">("signal_bar_close");
  readonly initialCash = input(100000);
  readonly commissionPerOrder = input(1);
  readonly tickerPool = input<readonly TickerOption[]>([]);
  readonly recentTickers = input<readonly string[]>([]);
  readonly running = input(false);
  readonly runBlocked = input(false);
  readonly launcherBlocksRun = input(false);
  readonly launcherStatus = input("unknown");
  readonly launcherDetail = input("");
  readonly launcherCommand = input("");
  readonly leanTemplateLabel = input<string | null>(null);

  readonly collapseToggled = output();
  readonly engineChanged = output<EngineChoice>();
  readonly strategySelected = output<string>();
  readonly parameterChanged = output<StrategyParameterChange>();
  readonly fillModeChanged = output<"signal_bar_close" | "next_bar_open">();
  readonly initialCashChanged = output<number>();
  readonly commissionChanged = output<number>();
  readonly launcherCheckRequested = output();
  readonly runRequested = output();

  readonly selectedStrategy = computed(() =>
    this.strategies().find((strategy) => strategy.name === this.selectedStrategyName()) ?? null,
  );
  readonly engineLabel = computed(() => {
    const engine = this.engine();
    return engine === "lean" ? "LEAN" : engine === "both" ? "Both" : "Python";
  });
  readonly strategyLabel = computed(() =>
    this.selectedStrategy()?.display_name ?? this.selectedStrategyName() ?? "No strategy",
  );
  readonly windowLabel = computed(() => `${this.range().from} → ${this.range().to}`);

  readonly parameterEntries = computed(() => {
    const properties = this.selectedStrategy()?.params_schema.properties ?? {};
    return Object.entries(properties)
      .filter(([field]) => field !== "symbol")
      .map(([field, property]) => ({ field, property }));
  });

  readonly samplingSummary = computed(() => {
    const policy = this.dataPolicy();
    const input = policy.input_bars;
    const sampling = `${input.multiplier} ${input.timespan}${input.multiplier === 1 ? "" : "s"}`;
    const session = policy.session === "regular" ? "regular session" : "extended session";
    const provider = policy.provider_kind === "fixture"
      ? `fixture ${policy.fixture_id ?? "(unidentified)"}`
      : policy.source;
    return `${sampling} · ${session} · ${provider}`;
  });

  readonly primaryAction = computed<StrategyLabPrimaryAction>(() => {
    const unavailableStrategy = this.selectedStrategy() === null;
    const recoveryDisabled = this.running() || !this.selectedStrategyName();
    if (this.launcherBlocksRun()) {
      return {
        kind: "check-launcher",
        label: "Check launcher",
        disabled: recoveryDisabled || unavailableStrategy || this.runBlocked(),
      };
    }
    return {
      kind: "run",
      label: "Run validation",
      disabled: recoveryDisabled || unavailableStrategy || this.runBlocked(),
    };
  });

  activatePrimaryAction(): void {
    if (this.primaryAction().disabled) return;
    if (this.primaryAction().kind === "check-launcher") {
      this.launcherCheckRequested.emit();
      return;
    }
    this.runRequested.emit();
  }

  selectEngine(engine: EngineChoice): void {
    this.engineChanged.emit(engine);
  }

  onStrategyEvent(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLSelectElement && target.value) this.strategySelected.emit(target.value);
  }

  onParameterEvent(field: string, type: string | undefined, event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) {
      this.parameterChanged.emit({ field, rawValue: target.value, type });
    }
  }

  onFillModeEvent(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLSelectElement && (target.value === "signal_bar_close" || target.value === "next_bar_open")) {
      this.fillModeChanged.emit(target.value);
    }
  }

  onInitialCashEvent(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) this.emitFiniteNumber(target.value, this.initialCashChanged);
  }

  onCommissionEvent(event: Event): void {
    const target = event.target;
    if (target instanceof HTMLInputElement) this.emitFiniteNumber(target.value, this.commissionChanged);
  }

  private emitFiniteNumber(value: string, emitter: { emit(value: number): void }): void {
    const parsed = Number(value);
    if (Number.isFinite(parsed) && parsed >= 0) emitter.emit(parsed);
  }
}
