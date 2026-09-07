import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from "@angular/core";
import { Router } from "@angular/router";
import { Drawer } from "primeng/drawer";
import { Tab, TabList, TabPanel, TabPanels, Tabs } from "primeng/tabs";

import { RunDockComponent } from "../../shared/run-dock/run-dock.component";
import {
  RUN_DOCK_SOURCE,
  RUN_DOCK_STORAGE_KEY,
} from "../../shared/run-dock/run-dock-source";
import { EngineLabRunHistoryComponent } from "../lean-engine/engine-lab-run-history/engine-lab-run-history.component";
import { EngineRunDockSource } from "../lean-engine/engine-run-dock-source";
import { LeanSourceEditorComponent } from "./lean-source-editor/lean-source-editor.component";
import { StrategyLabConfigRailComponent } from "./strategy-lab-config-rail/strategy-lab-config-rail.component";
import { StrategyLabConfigStore } from "./strategy-lab-config.store";
import { JobsService } from "../../services/jobs.service";
import { StrategyLabRunner, type AdoptedJob } from "./strategy-lab-runner.service";
import { StrategyLabRunReport } from "./strategy-lab-run-report.service";
import { StrategyLabRunStatsComponent } from "./run-stats/strategy-lab-run-stats.component";
import { StrategyLabStageComponent } from "./strategy-lab-stage/strategy-lab-stage.component";
import { inputsFromBacktestJob, inputsFromSavedRun, type StrategyLabRunInputs } from "./strategy-lab.models";

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
    Drawer,
    StrategyLabConfigRailComponent,
    EngineLabRunHistoryComponent,
    LeanSourceEditorComponent,
    RunDockComponent,
    StrategyLabRunStatsComponent,
    StrategyLabStageComponent,
  ],
  templateUrl: "./strategy-lab.component.html",
  styleUrl: "./strategy-lab.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
  providers: [
    StrategyLabConfigStore,
    StrategyLabRunner,
    StrategyLabRunReport,
    EngineRunDockSource,
    { provide: RUN_DOCK_SOURCE, useExisting: EngineRunDockSource },
    { provide: RUN_DOCK_STORAGE_KEY, useValue: "run-dock-expanded:strategy-lab" },
  ],
})
export class StrategyLabComponent {
  private readonly router = inject(Router);
  readonly config = inject(StrategyLabConfigStore);
  readonly runs = inject(StrategyLabRunner);
  readonly jobs = inject(JobsService);
  readonly report = inject(StrategyLabRunReport);

  protected readonly leanSourceOpen = signal(false);

  /**
   * The report resource re-emits a new run object on every 5s poll. Collapsing
   * it to the run's identity means downstream reacts once per distinct run
   * instead of once per poll — signal equality is the guard, so no field has
   * to remember which run was already adopted.
   */
  private readonly loadedRun = computed(() => this.report.run());
  private readonly loadedRunId = computed(() => this.loadedRun()?.id ?? null);

  constructor() {
    const strategiesReady = this.config.loadStrategies();

    // The URL is the report's only input. Setting a signal to the value it
    // already holds is a no-op, so a repeated `?run=N` costs nothing and
    // `?run=` disappearing (browser Back) clears the run off the page.
    effect(() => {
      const runId = this.config.activeRunParam();
      this.report.activeRunId.set(runId);
      if (runId !== null) {
        this.runs.clearRunError();
        // Switch to the workbench synchronously on the URL change, not after
        // the fetch resolves in `adoptRun` — a run picked from History that
        // fails to load (deleted, transient GraphQL error) must still land
        // the operator where the error notice renders, or the click is a
        // silent dead end.
        this.config.activeTab.set("configuration");
      }
    });

    effect(() => {
      const job = this.runs.adoptedJob();
      if (job === null) return;
      untracked(() => void this.describeAdoptedJob(job, strategiesReady));
    });

    effect(() => {
      if (this.loadedRunId() === null) return;
      // The run id is the whole tracked dependency. Everything adoption does
      // — reading and clearing `justProducedRunId`, collapsing the rail,
      // rewriting the configuration — writes signals this effect must not
      // also depend on, or it would retrigger itself.
      untracked(() => void this.adoptRun(strategiesReady));
    });
  }

  selectHistoryRun(id: string): void {
    const numericId = Number(id);
    if (!Number.isInteger(numericId) || numericId <= 0) {
      this.runs.runError.set("That saved run has an invalid identifier.");
      return;
    }

    void this.router.navigate(["/strategy-lab"], {
      queryParams: { run: numericId },
      queryParamsHandling: "merge",
    });
  }

  /**
   * Bring the configuration on screen in line with the run the report just
   * loaded. A transport or "no such run" failure is the report's own state
   * (`loadError` / `notFound`) and never reaches here — so it can never set
   * the rerun-blocking `configurationWarning`, which would disable the Run
   * button for a configuration that is still perfectly valid.
   */
  private async adoptRun(strategiesReady: Promise<void>): Promise<void> {
    const run = this.loadedRun();
    if (run === null) return;
    // Transient only: `configNavOverride` is the operator's saved preference
    // and a completed run is an event, not a setting.
    this.config.configNavCollapsed.set(true);
    // The persisted fields are restored even for a run this page just
    // produced: the operator can have changed still-enabled controls while
    // the job ran, and the report must not render beside inputs that no
    // longer describe it. The one thing the restore would destroy is the
    // submitted QCAlgorithm — `restoreStrategy` → `applyStrategy` nulls
    // `customLeanSource`, and custom source is the only route to a
    // parameterized LEAN run — so carry that across instead of skipping.
    const justProduced = this.runs.justProducedRunId() === run.id;
    const submittedSource = justProduced ? this.config.customLeanSource() : null;
    await strategiesReady;
    // `activeRunParam` can move while the strategy catalog loads. A stale
    // invocation must not restore its run's configuration over the run now
    // selected, or over no run at all.
    if (this.report.activeRunId() !== run.id) return;
    if (justProduced) this.runs.justProducedRunId.set(null);
    try {
      this.applyRunInputs(inputsFromSavedRun(run, this.config.range()));
      this.runs.runError.set(null);
      if (submittedSource !== null) this.config.customLeanSource.set(submittedSource);
    } catch (error) {
      const message = error instanceof Error
        ? error.message
        : "The saved configuration could not be restored.";
      this.config.configurationWarning.set(message);
      this.runs.runError.set(message);
    }
  }

  /**
   * Bring the rail in line with a job this tab adopted rather than started
   * (#1953). After a reload the store is rebuilt with defaults while the
   * resumed job runs with the inputs it was submitted with, so the rail
   * labelled "Exact run inputs" would describe the defaults until the
   * persisted report reconciled them. A loaded report keeps the rail — its
   * inputs describe what is on the stage — and a payload this workbench
   * cannot read leaves the rail alone rather than guessing.
   */
  private async describeAdoptedJob(job: AdoptedJob, strategiesReady: Promise<void>): Promise<void> {
    const inputs = inputsFromBacktestJob(job.parameters, this.config.range());
    if (inputs === null) return;
    await strategiesReady;
    if (this.report.activeRunId() !== null) return;
    this.applyRunInputs(inputs);
  }

  private applyRunInputs(inputs: StrategyLabRunInputs): void {
    this.config.engine.set(inputs.engine);
    this.config.range.set(inputs.range);
    this.config.restoreStrategy(inputs.strategyName);
    this.config.paramValues.set({ ...this.config.paramValues(), ...inputs.parameters });
    this.config.fillMode.set(inputs.fillMode);
    this.config.initialCash.set(inputs.initialCash);
    this.config.commissionPerOrder.set(inputs.commissionPerOrder);
    this.config.restoreDataPolicy(inputs.dataPolicy);
  }

  selectStrategy(name: string): void {
    this.config.selectStrategy(name);
    this.runs.clearRunError();
  }
}
