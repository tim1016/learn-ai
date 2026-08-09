import { ChangeDetectionStrategy, Component, inject, signal } from "@angular/core";
import { firstValueFrom } from "rxjs";
import { Apollo } from "apollo-angular";
import { Tab, TabList, TabPanel, TabPanels, Tabs } from "primeng/tabs";

import { PageHeaderComponent } from "../../shared/page-header/page-header.component";
import { RunDockComponent } from "../../shared/run-dock/run-dock.component";
import {
  RUN_DOCK_SOURCE,
  RUN_DOCK_STORAGE_KEY,
} from "../../shared/run-dock/run-dock-source";
import {
  BACKTEST_RUN_DETAIL_QUERY,
  type BacktestRunDetail,
  type BacktestRunDetailQueryResult,
} from "../../graphql/backtest-runs.query";
import { RunReportComponent } from "../engine-lab/run-report/run-report.component";
import { EngineLabRunHistoryComponent } from "../lean-engine/engine-lab-run-history/engine-lab-run-history.component";
import { EngineRunDockSource } from "../lean-engine/engine-run-dock-source";
import { ValidationStagePlaceholderComponent } from "../lean-engine/validation-stage-placeholder/validation-stage-placeholder.component";
import { StrategyLabConfigRailComponent } from "./strategy-lab-config-rail/strategy-lab-config-rail.component";
import type { TickerRange } from "../../shared/ticker-range-picker";
import type { DataPolicy } from "../../models/data-policy";
import { StrategyLabConfigStore } from "./strategy-lab-config.store";
import { StrategyLabRunner } from "./strategy-lab-runner.service";
import { parseStrategyParameters, type EngineChoice } from "./strategy-lab.models";

export interface StrategyLabConfiguration {
  engine: EngineChoice;
  range: TickerRange;
  parameters: Record<string, unknown>;
  fillMode: "signal_bar_close" | "next_bar_open";
  initialCash: number;
  commissionPerOrder: number;
  dataPolicy: DataPolicy | null;
}

/**
 * Strategy Lab's focused product shell. Configuration and run orchestration
 * are deliberately composed as separate component-scoped services so this
 * screen cannot inherit state or effects from a retired product surface.
 */
@Component({
  selector: "app-strategy-lab",
  imports: [
    PageHeaderComponent,
    Tabs,
    TabList,
    Tab,
    TabPanels,
    TabPanel,
    StrategyLabConfigRailComponent,
    EngineLabRunHistoryComponent,
    ValidationStagePlaceholderComponent,
    RunReportComponent,
    RunDockComponent,
  ],
  templateUrl: "./strategy-lab.component.html",
  styleUrl: "./strategy-lab.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [
    StrategyLabConfigStore,
    StrategyLabRunner,
    EngineRunDockSource,
    { provide: RUN_DOCK_SOURCE, useExisting: EngineRunDockSource },
    { provide: RUN_DOCK_STORAGE_KEY, useValue: "run-dock-expanded:strategy-lab" },
  ],
})
export class StrategyLabComponent {
  private readonly apollo = inject(Apollo);
  readonly config = inject(StrategyLabConfigStore);
  readonly runs = inject(StrategyLabRunner);
  readonly selectedRun = signal<BacktestRunDetail | null>(null);

  constructor() {
    void this.config.loadStrategies();
  }

  async selectHistoryRun(id: string): Promise<void> {
    const numericId = Number(id);
    if (!Number.isInteger(numericId) || numericId <= 0) {
      this.runs.runError.set("That saved run has an invalid identifier.");
      return;
    }

    try {
      const response = await firstValueFrom(
        this.apollo.query<BacktestRunDetailQueryResult>({
          query: BACKTEST_RUN_DETAIL_QUERY,
          variables: { id: numericId },
          fetchPolicy: "network-only",
        }),
      );
      const run = response.data?.backtestRun;
      if (run === null || run === undefined) {
        this.runs.runError.set(`Saved run #${numericId} was not found.`);
        return;
      }
      this.runs.completedRunId.set(run.id);
      this.selectedRun.set(run);
      this.config.activeTab.set("configuration");
      try {
        this.restoreConfiguration(run);
      } catch (error) {
        const message = error instanceof Error
          ? error.message
          : "The saved configuration could not be restored.";
        this.config.configurationWarning.set(message);
        this.runs.runError.set(message);
      }
    } catch (error) {
      this.runs.runError.set(error instanceof Error ? error.message : "Failed to load the saved run.");
    }
  }

  private restoreConfiguration(run: BacktestRunDetail): void {
    const configuration = toStrategyLabConfiguration(run, this.config.range());

    this.config.engine.set(configuration.engine);
    this.config.range.set(configuration.range);
    this.config.restoreStrategy(run.strategyName);
    this.config.paramValues.set({ ...this.config.paramValues(), ...configuration.parameters });
    this.config.fillMode.set(configuration.fillMode);
    this.config.initialCash.set(configuration.initialCash);
    this.config.commissionPerOrder.set(configuration.commissionPerOrder);
    this.config.restoreDataPolicy(configuration.dataPolicy);
    this.runs.runError.set(null);
  }

  selectStrategy(name: string): void {
    this.config.selectStrategy(name);
    this.runs.clearReport();
    this.selectedRun.set(null);
  }
}

export function toStrategyLabConfiguration(
  run: BacktestRunDetail,
  currentRange: TickerRange,
): StrategyLabConfiguration {
  const parameters = parseStrategyParameters(run.parameters);
  const symbol = run.dataPolicy?.symbol ?? run.symbol ?? readString(parameters, "symbol") ?? "SPY";
  const policy = run.dataPolicy;
  const timespan = policy?.input_bars.timespan;
  return {
    engine: run.requestedEngine ?? inferRequestedEngine(run),
    range: {
      ...currentRange,
      symbol: symbol.toUpperCase(),
      from: run.startDate,
      to: run.endDate,
      resolution: timespan === "day" ? "daily" : timespan ?? "minute",
      multiplier: policy?.input_bars.multiplier ?? 1,
      session: policy?.session === "extended" ? "extended" : "rth",
      autoFetch: policy?.provider_kind !== "fixture",
    },
    parameters: { ...parameters, symbol: symbol.toUpperCase() },
    fillMode: run.fillMode === "next_bar_open" ? "next_bar_open" : "signal_bar_close",
    initialCash: run.initialCash,
    commissionPerOrder: run.commissionPerOrder ?? 0,
    dataPolicy: policy ?? null,
  };
}

function readString(value: Record<string, unknown>, key: string): string | null {
  const candidate = value[key];
  return typeof candidate === "string" && candidate.trim() ? candidate : null;
}

function inferRequestedEngine(run: BacktestRunDetail): EngineChoice {
  return run.engine === "LEAN" || run.source === "lean-sidecar" ? "lean" : "python";
}
