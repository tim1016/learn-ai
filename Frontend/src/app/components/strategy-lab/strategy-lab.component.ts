import { ChangeDetectionStrategy, Component, effect, inject } from "@angular/core";
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
import {
  LeanEngineController,
  type EngineChoice,
} from "../lean-engine/lean-engine.component";
import { EngineLabRunHistoryComponent } from "../lean-engine/engine-lab-run-history/engine-lab-run-history.component";
import { EngineRunDockSource } from "../lean-engine/engine-run-dock-source";
import { ValidationStagePlaceholderComponent } from "../lean-engine/validation-stage-placeholder/validation-stage-placeholder.component";
import { StrategyLabConfigRailComponent } from "./strategy-lab-config-rail/strategy-lab-config-rail.component";
import type { TickerRange } from "../../shared/ticker-range-picker";

export interface StrategyLabConfiguration {
  engine: EngineChoice;
  range: TickerRange;
  parameters: Record<string, unknown>;
  fillMode: "signal_bar_close" | "next_bar_open";
  initialCash: number;
  commissionPerOrder: number;
}

/**
 * Strategy Lab's focused product shell. The headless controller preserves
 * the proven job orchestration while this component owns the canonical,
 * two-tab information architecture and exact History rehydration.
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
    EngineRunDockSource,
    { provide: RUN_DOCK_SOURCE, useExisting: EngineRunDockSource },
    { provide: RUN_DOCK_STORAGE_KEY, useValue: "run-dock-expanded:strategy-lab" },
  ],
})
export class StrategyLabComponent extends LeanEngineController {
  private readonly apollo = inject(Apollo);

  constructor() {
    super();
    effect(() => {
      if (this.activeTab() !== "configuration" && this.activeTab() !== "history") {
        this.activeTab.set("configuration");
      }
    });
  }

  async selectHistoryRun(id: string): Promise<void> {
    const numericId = Number(id);
    if (!Number.isInteger(numericId) || numericId <= 0) {
      this.runError.set("That saved run has an invalid identifier.");
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
        this.runError.set(`Saved run #${numericId} was not found.`);
        return;
      }
      this.restoreConfiguration(run);
    } catch (error) {
      this.runError.set(error instanceof Error ? error.message : "Failed to load the saved run.");
    }
  }

  private restoreConfiguration(run: BacktestRunDetail): void {
    const configuration = toStrategyLabConfiguration(run, this.rangeState());

    this.engine.set(configuration.engine);
    this.rangeState.set(configuration.range);
    this.onStrategyChange(run.strategyName);
    this.paramValues.set({ ...this.paramValues(), ...configuration.parameters });
    this.fillMode.set(configuration.fillMode);
    this.initialCash.set(configuration.initialCash);
    this.commissionPerOrder.set(configuration.commissionPerOrder);
    this.completedRunId.set(run.id);
    this.activeTab.set("configuration");
    this.runError.set(null);
  }
}

export function toStrategyLabConfiguration(
  run: BacktestRunDetail,
  currentRange: TickerRange,
): StrategyLabConfiguration {
  const parameters = parseRunParameters(run.parameters);
  const symbol = run.dataPolicy?.symbol ?? run.symbol ?? readString(parameters, "symbol") ?? "SPY";
  const timespan = run.dataPolicy?.input_bars.timespan;
  return {
    engine: run.requestedEngine ?? inferRequestedEngine(run),
    range: {
      ...currentRange,
      symbol: symbol.toUpperCase(),
      from: run.startDate,
      to: run.endDate,
      resolution: timespan === "day" ? "daily" : "minute",
      autoFetch: true,
    },
    parameters: { ...parameters, symbol: symbol.toUpperCase() },
    fillMode: run.fillMode === "next_bar_open" ? "next_bar_open" : "signal_bar_close",
    initialCash: run.initialCash,
    commissionPerOrder: run.commissionPerOrder ?? 0,
  };
}

function parseRunParameters(value: string | null): Record<string, unknown> {
  if (!value) return {};
  try {
    const parsed: unknown = JSON.parse(value);
    return parsed !== null && typeof parsed === "object" && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function readString(value: Record<string, unknown>, key: string): string | null {
  const candidate = value[key];
  return typeof candidate === "string" && candidate.trim() ? candidate : null;
}

function inferRequestedEngine(run: BacktestRunDetail): EngineChoice {
  return run.engine === "LEAN" || run.source === "lean-sidecar" ? "lean" : "python";
}
