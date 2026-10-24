import {
  ChangeDetectionStrategy,
  Component,
  computed,
  effect,
  inject,
  signal,
  untracked,
} from "@angular/core";
import { Router, RouterLink } from "@angular/router";
import { Drawer } from "primeng/drawer";
import { Tab, TabList, TabPanel, TabPanels, Tabs } from "primeng/tabs";

import { EngineLabRunHistoryComponent } from "../../../components/lean-engine/engine-lab-run-history/engine-lab-run-history.component";
import { GoldenValidationWorkbenchComponent } from "../../../components/golden-validation-workbench/golden-validation-workbench.component";
import { LeanSourceEditorComponent } from "../../../components/strategy-lab/lean-source-editor/lean-source-editor.component";
import { StrategyLabRunStatsComponent } from "../../../components/strategy-lab/run-stats/strategy-lab-run-stats.component";
import { StrategyLabConfigRailComponent } from "../../../components/strategy-lab/strategy-lab-config-rail/strategy-lab-config-rail.component";
import { StrategyLabConfigStore } from "../../../components/strategy-lab/strategy-lab-config.store";
import { StrategyLabRunReport } from "../../../components/strategy-lab/strategy-lab-run-report.service";
import { StrategyLabRunner, type AdoptedJob } from "../../../components/strategy-lab/strategy-lab-runner.service";
import { StrategyLabStageComponent } from "../../../components/strategy-lab/strategy-lab-stage/strategy-lab-stage.component";
import {
  inputsFromBacktestJob,
  inputsFromSavedRun,
  type StrategyLabRunInputs,
} from "../../../components/strategy-lab/strategy-lab.models";
import { JobsService } from "../../../services/jobs.service";
import { RunDockComponent } from "../../../shared/run-dock/run-dock.component";
import type { WorkspaceStateContent } from "../../../shared/workspace/workspace-state";
import { WorkspaceStateBlockComponent } from "../../../shared/workspace/workspace-state-block.component";
import { EXPLORE_RUN_PARAM, exploreTabRoute } from "../explore-tabs";
import { ExploreSession } from "../explore-session";

/**
 * Explore's Backtest: Strategy Lab's workbench and run history for one strategy.
 * Configuration, the runner and the report are the Explore scope's, not this
 * page's: a backtest in flight belongs to the strategy, so leaving for Sweep,
 * Walk-forward or Define and coming back finds the same run still going, never a
 * second runner for the same work. Nothing here approves settings; a run is
 * exploratory and in-sample.
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
    RouterLink,
    StrategyLabConfigRailComponent,
    EngineLabRunHistoryComponent,
    LeanSourceEditorComponent,
    RunDockComponent,
    StrategyLabRunStatsComponent,
    StrategyLabStageComponent,
    GoldenValidationWorkbenchComponent,
    WorkspaceStateBlockComponent,
  ],
  templateUrl: "./explore-backtest.component.html",
  styleUrl: "./explore-backtest.component.scss",
  changeDetection: ChangeDetectionStrategy.OnPush,
})
export class ExploreBacktestComponent {
  private readonly router = inject(Router);
  private readonly session = inject(ExploreSession);
  readonly config = inject(StrategyLabConfigStore);
  readonly runs = inject(StrategyLabRunner);
  readonly jobs = inject(JobsService);
  readonly report = inject(StrategyLabRunReport);

  protected readonly key = this.session.key;
  protected readonly sweepRoute = computed(() => exploreTabRoute(this.key(), "sweep"));
  protected readonly walkForwardRoute = computed(() => exploreTabRoute(this.key(), "walk-forward"));
  protected readonly sweepCapability = this.session.sweepCapability;

  protected readonly leanSourceOpen = signal(false);
  protected readonly goldenCandidateRunId = signal<number | null>(null);
  protected readonly goldenRefreshToken = signal(0);

  /**
   * The report resource re-emits a new run object on every 5s poll. Collapsing
   * it to the run's identity means downstream reacts once per distinct run
   * instead of once per poll — signal equality is the guard, so no field has
   * to remember which run was already adopted. Only a run this strategy
   * produced is adopted: another strategy's run names its owner instead.
   */
  private readonly loadedRun = computed(() => this.report.run());
  private readonly loadedRunId = computed(() => {
    const run = this.loadedRun();
    return run !== null && run.strategyName === this.key() ? run.id : null;
  });

  /** The run on screen, only when this strategy produced it. */
  protected readonly shownRun = computed(() => {
    const run = this.report.displayRun();
    return run !== null && run.strategyName === this.key() ? run : null;
  });
  protected readonly markers = computed(() => (this.shownRun() === null ? [] : this.report.markers()));
  protected readonly equityPoints = computed(() => (this.shownRun() === null ? [] : this.report.equityPoints()));
  protected readonly reportNotices = computed(() => (this.shownRun() === null ? [] : this.report.reportNotices()));

  /** A run the address names that another strategy produced: never drawn under this strategy's header. */
  protected readonly mismatch = computed<WorkspaceStateContent | null>(() => {
    const run = this.loadedRun();
    if (run === null || run.strategyName === this.key()) return null;
    return {
      kind: "unavailable",
      happened: `Saved run #${run.id} belongs to ${run.strategyName}, not ${this.key()}.`,
      why: "A saved run opens only under the strategy that produced it, so this page shows none of its report.",
      next: "Open it under its own strategy, or go back to this strategy's backtests.",
      actions: [
        {
          id: "owner",
          label: `Open run #${run.id} under ${run.strategyName}`,
          route: exploreTabRoute(run.strategyName, "backtest"),
          queryParams: { [EXPLORE_RUN_PARAM]: String(run.id) },
        },
        { id: "own", label: `Back to ${this.key()}`, route: exploreTabRoute(this.key(), "backtest") },
      ],
    };
  });

  constructor() {
    const strategiesReady = this.config.ensureStrategiesLoaded();

    // The tab strip leads back to the run this tool last showed.
    effect(() => {
      const runId = this.config.activeRunParam();
      if (runId !== null) untracked(() => this.session.backtestRunId.set(runId));
    });

    // A run this runner just saved is opened here, where it is shown. Finished
    // while another tool was open, it waits: the owner is not pulled back.
    effect(() => {
      const runId = this.runs.pendingOpenRunId();
      if (runId === null) return;
      untracked(() => {
        this.runs.pendingOpenRunId.set(null);
        void this.openRun(runId);
      });
    });

    // What Define shows as the values being tried out.
    effect(() => {
      const strategy = this.config.selectedStrategy();
      if (strategy === null) return;
      const range = this.config.range();
      const values = this.config.paramValues();
      const changed = new Set(this.config.changedParameterNames());
      const engine = this.config.engine();
      const resolution = this.config.resolution();
      const restoredFrom = this.loadedRunId();
      untracked(() =>
        this.session.exploratory.set({
          symbol: this.config.effectiveSymbol(),
          from: range.from,
          to: range.to,
          resolution,
          engine,
          parameters: Object.keys(strategy.params_schema.properties ?? {})
            .filter((name) => name !== "symbol")
            .map((name) => ({ name, value: values[name], changedFromDefault: changed.has(name) })),
          restoredFromRunId: restoredFrom,
        }),
      );
    });

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
        // fails to load (deleted, transient server error) must still land
        // the operator where the error notice renders, or the click is a
        // silent dead end.
        this.config.activeTab.set("configuration");
      }
    });

    effect(() => {
      // Re-evaluated when the adopted job ends, when a report leaves the
      // stage (Back, a load that fails), and once launch parameters are
      // applied — so the job's inputs are neither hidden behind a report
      // that is gone nor reset by a later launch-parameter application.
      const job = this.runs.adoptedJob();
      if (job === null || this.report.runOnStage() || !this.config.launchParamsSettled()) return;
      untracked(() => void this.describeAdoptedJob(job, strategiesReady));
    });

    effect(() => {
      // A saved run named in the link wins over the link's launch parameters
      // (#2872): its restore waits until they have settled, so they are never
      // applied over it, whichever of the run and the strategy list answers
      // first.
      if (this.loadedRunId() === null || !this.config.launchParamsSettled()) return;
      // The run id and that gate are the whole tracked dependency. Everything
      // adoption does — reading and clearing `justProducedRunId`, collapsing
      // the rail, rewriting the configuration — writes signals this effect
      // must not also depend on, or it would retrigger itself.
      untracked(() => void this.adoptRun(strategiesReady));
    });
  }

  selectHistoryRun(id: string): void {
    const numericId = Number(id);
    if (!Number.isInteger(numericId) || numericId <= 0) {
      this.runs.runError.set("That saved run has an invalid identifier.");
      return;
    }

    void this.openRun(numericId);
  }

  private openRun(runId: number): Promise<boolean> {
    return this.router.navigate([...exploreTabRoute(this.key(), "backtest")], {
      queryParams: { [EXPLORE_RUN_PARAM]: runId },
      queryParamsHandling: "merge",
    });
  }

  selectGoldenCandidate(runId: number): void {
    this.goldenCandidateRunId.set(runId);
  }

  refreshGoldenHistory(): void {
    this.goldenRefreshToken.update((token) => token + 1);
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
    // Opened again from another tool: the inputs were taken over already, and
    // whatever was edited since stays.
    if (this.session.adoptedBacktestRunId() === run.id) return;
    this.session.adoptedBacktestRunId.set(run.id);
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
   * persisted report reconciled them. A report on the stage keeps the rail —
   * its inputs describe what is shown — and a payload this workbench cannot
   * read leaves the rail alone rather than guessing.
   */
  private async describeAdoptedJob(job: AdoptedJob, strategiesReady: Promise<void>): Promise<void> {
    const inputs = inputsFromBacktestJob(job.parameters, this.config.range());
    if (inputs === null) return;
    await strategiesReady;
    if (this.runs.adoptedJob() !== job || this.report.runOnStage()) return;
    // Described once: coming back from another tool keeps what was edited since.
    if (this.session.describedJobId() === job.id) return;
    this.session.describedJobId.set(job.id);
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
