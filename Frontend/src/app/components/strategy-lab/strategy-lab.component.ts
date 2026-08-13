import { ChangeDetectionStrategy, Component, inject } from "@angular/core";
import { ActivatedRoute, Router } from "@angular/router";
import { firstValueFrom } from "rxjs";
import { Apollo } from "apollo-angular";
import { Tab, TabList, TabPanel, TabPanels, Tabs } from "primeng/tabs";

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
import { EngineLabRunHistoryComponent } from "../lean-engine/engine-lab-run-history/engine-lab-run-history.component";
import { EngineRunDockSource } from "../lean-engine/engine-run-dock-source";
import { ValidationStagePlaceholderComponent } from "../lean-engine/validation-stage-placeholder/validation-stage-placeholder.component";
import { StrategyLabConfigRailComponent } from "./strategy-lab-config-rail/strategy-lab-config-rail.component";
import { StrategyLabConfigStore } from "./strategy-lab-config.store";
import { StrategyLabRunner } from "./strategy-lab-runner.service";
import { toStrategyLabConfiguration } from "./strategy-lab.models";

/**
 * Strategy Lab's focused product shell. Configuration and run orchestration
 * are deliberately composed as separate component-scoped services so this
 * screen cannot inherit state or effects from a retired product surface.
 */
@Component({
  selector: "app-strategy-lab",
  imports: [
    Tabs,
    TabList,
    Tab,
    TabPanels,
    TabPanel,
    StrategyLabConfigRailComponent,
    EngineLabRunHistoryComponent,
    ValidationStagePlaceholderComponent,
    RunDockComponent,
  ],
  templateUrl: "./strategy-lab.component.html",
  styleUrl: "./strategy-lab.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
  host: { class: 'page-inset' },
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
  private readonly router = inject(Router);
  private readonly route = inject(ActivatedRoute);
  readonly config = inject(StrategyLabConfigStore);
  readonly runs = inject(StrategyLabRunner);

  private readonly restoreRunId = parseRunId(this.route.snapshot.queryParamMap.get("restoreRun"));

  constructor() {
    const strategiesReady = this.config.loadStrategies();
    if (this.restoreRunId !== null) {
      this.runs.clearRunError();
      void this.restoreSavedRun(this.restoreRunId, strategiesReady);
    }
  }

  selectHistoryRun(id: string): void {
    const numericId = Number(id);
    if (!Number.isInteger(numericId) || numericId <= 0) {
      this.runs.runError.set("That saved run has an invalid identifier.");
      return;
    }

    void this.router.navigate(["/strategy-lab/runs", numericId]);
  }

  private async restoreSavedRun(runId: number, strategiesReady: Promise<void>): Promise<void> {
    await strategiesReady;
    try {
      const response = await firstValueFrom(
        this.apollo.query<BacktestRunDetailQueryResult>({
          query: BACKTEST_RUN_DETAIL_QUERY,
          variables: { id: runId },
          fetchPolicy: "network-only",
        }),
      );
      const run = response.data?.backtestRun;
      if (run === null || run === undefined) {
        this.runs.runError.set(`Saved run #${runId} was not found.`);
        return;
      }
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
    this.runs.clearRunError();
  }
}

function parseRunId(value: string | null): number | null {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed > 0 ? parsed : null;
}
