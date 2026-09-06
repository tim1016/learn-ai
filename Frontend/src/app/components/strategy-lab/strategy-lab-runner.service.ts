import { HttpErrorResponse } from "@angular/common/http";
import { computed, effect, inject, Injectable, signal } from "@angular/core";
import { Router } from "@angular/router";

import { JobsService, type JobState } from "../../services/jobs.service";
import { LeanSidecarService } from "../../services/lean-sidecar.service";
import type {
  LeanLauncherDiagnosticReport,
  TrustedRunResponse,
} from "../../services/lean-sidecar.types";
import { StrategyLabConfigStore } from "./strategy-lab-config.store";
import {
  previousIsoDate,
  type EngineBacktestResponse,
  type LeanLauncherStatus,
  type StrategyLabRunPhase,
} from "./strategy-lab.models";

const COMPATIBILITY_PROFILE = "us-equity-raw-ibkr-v1";

/**
 * The job this browser tab started, kept in `sessionStorage` (per-tab,
 * survives a reload, never shared with other tabs) so a reload reattaches
 * to *this* tab's run even when other backtests are active. Storage is a
 * convenience, not execution state: when it is unavailable the runner
 * falls back to adopting any active job of the right type.
 */
const OWN_JOB_KEY = "strategyLab.ownJobId";

/** Every LEAN run this workbench submits carries this `run_id` prefix; the
 *  parity companion submits `lean_engine_run` jobs through the same public
 *  route with a `companion-…` id, and those must never be adopted here. */
const STRATEGY_LAB_RUN_ID_PREFIX = "strategy_lab_";

function isStrategyLabJob(job: JobState): boolean {
  if (job.type === "engine_backtest") return true;
  if (job.type !== "lean_engine_run") return false;
  const request = job.parameters?.["request"];
  const runId = typeof request === "object" && request !== null ? (request as Record<string, unknown>)["run_id"] : undefined;
  // Parameters can be missing for old or malformed server state; a job whose
  // owner cannot be established is left alone rather than guessed.
  return typeof runId === "string" && runId.startsWith(STRATEGY_LAB_RUN_ID_PREFIX);
}

function rememberOwnJob(id: string): void {
  try {
    if (typeof sessionStorage === "undefined") return;
    sessionStorage.setItem(OWN_JOB_KEY, id);
  } catch {
    // Denied storage only loses the marker; `wireJobAdoptionEffect` then
    // reattaches solely to an unambiguous single job, never a guess.
  }
}

function ownJobId(): string | null {
  try {
    if (typeof sessionStorage === "undefined") return null;
    return sessionStorage.getItem(OWN_JOB_KEY);
  } catch {
    return null;
  }
}

function forgetOwnJob(): void {
  try {
    if (typeof sessionStorage === "undefined") return;
    sessionStorage.removeItem(OWN_JOB_KEY);
  } catch {
    // A marker that outlives its job is ignored by the reattach rule anyway.
  }
}

@Injectable()
export class StrategyLabRunner {
  private readonly jobs = inject(JobsService);
  private readonly leanSidecar = inject(LeanSidecarService);
  private readonly router = inject(Router);
  private readonly engineJobId = signal<string | null>(null);
  private readonly leanJobId = signal<string | null>(null);

  readonly config = inject(StrategyLabConfigStore);
  readonly leanLauncherCommand =
    "cd PythonDataService && PYTHONPATH=. ./.venv/bin/python -m uvicorn app.lean_sidecar.launcher.app:app --host 0.0.0.0 --port 8090";
  readonly leanLauncherStatus = signal<LeanLauncherStatus>("unknown");
  readonly leanLauncherDetail = signal("");
  readonly leanLauncherBlocksRun = computed(
    () => this.config.engine() !== "python" && this.leanLauncherStatus() !== "ready",
  );
  readonly running = signal(false);
  /** A Strategy Lab job is in flight somewhere — this tab's or another's.
   *  The rail keeps Run disabled on it, so ambiguity between several
   *  experiments never re-enables a third backtest on the busy container. */
  readonly engineBusy = computed(() => this.jobs.activeJobs().some(isStrategyLabJob));
  readonly runPhase = signal<StrategyLabRunPhase>("idle");
  readonly runStatusBanner = signal("");
  readonly runPhaseDetail = signal("");
  readonly runError = signal<string | null>(null);
  /** The run this runner just persisted, so the workbench can skip a restore
   *  that would clobber the configuration which produced it. */
  readonly justProducedRunId = signal<number | null>(null);
  /** True once this runner has started or adopted a job — see `wireJobAdoptionEffect`. */
  private adoptionSettled = false;

  constructor() {
    this.wireJobAdoptionEffect();
    this.wireEngineJobEffect();
    this.wireLeanJobEffect();
  }

  clearRunError(): void {
    this.runError.set(null);
  }

  async run(): Promise<void> {
    if (this.config.rerunBlocked()) {
      this.fail(
        "Saved configuration cannot be rerun",
        this.config.configurationWarning() ??
          "The selected strategy is no longer available for this engine and resolution.",
      );
      return;
    }
    const engine = this.config.engine();
    if (engine !== "python") {
      if (this.config.leanValidationTemplate() === null) {
        this.fail(
          "LEAN validation template unavailable",
          "Select a strategy with an aligned LEAN validation template.",
        );
        return;
      }
      if (
        engine === "lean" &&
        this.config.customLeanSource() !== null &&
        !this.config.customLeanSource()?.trim()
      ) {
        this.fail(
          "Custom QCAlgorithm source unavailable",
          "Load or enter a QCAlgorithm before running custom source.",
        );
        return;
      }
      if (!(await this.ensureLeanLauncherReady())) return;
    }
    if (engine === "lean") {
      await this.runLean();
      return;
    }
    await this.runPython();
  }

  async checkLeanLauncher(): Promise<void> {
    this.leanLauncherStatus.set("checking");
    this.leanLauncherDetail.set("");
    try {
      this.applyLeanLauncherReport(await this.leanSidecar.diagnose());
    } catch (error) {
      this.leanLauncherStatus.set("blocked");
      this.leanLauncherDetail.set(
        error instanceof Error ? error.message : "Launcher check failed.",
      );
    }
  }

  composeRunId(): string {
    const symbol = this.config.effectiveSymbol().toLowerCase().replace(/[^a-z0-9]/g, "");
    return `${STRATEGY_LAB_RUN_ID_PREFIX}${symbol}_${Date.now().toString(36)}`;
  }

  private async ensureLeanLauncherReady(): Promise<boolean> {
    await this.checkLeanLauncher();
    if (this.leanLauncherStatus() === "ready") return true;
    this.fail(
      "LEAN launcher unavailable",
      this.leanLauncherDetail() || "Start the LEAN launcher before running.",
    );
    return false;
  }

  private async runPython(): Promise<void> {
    const strategyName = this.config.selectedStrategyName();
    if (!strategyName) return;
    this.beginRun(
      "Submitting backtest…",
      `${this.config.effectiveSymbol()} · ${this.config.startDate()} → ${this.config.endDate()}`,
    );
    const backtest: Record<string, unknown> = {
      strategy_name: strategyName,
      requested_engine: this.config.engine(),
      fill_mode: this.config.fillMode(),
      initial_cash: this.config.initialCash(),
      commission_per_order: this.config.commissionPerOrder(),
      params: this.config.paramValues(),
      auto_fetch: this.config.autoFetch(),
      resolution: this.config.resolution(),
      compatibility_profile:
        this.config.engine() === "both" ? COMPATIBILITY_PROFILE : null,
      data_policy: this.config.dataPolicy(),
      start_date: this.config.startDate(),
      end_date: this.config.endDate(),
    };
    try {
      const jobId = await this.jobs.startJob("engine_backtest", { backtest });
      rememberOwnJob(jobId);
      this.adoptionSettled = true;
      this.engineJobId.set(jobId);
    } catch (error) {
      const detail = httpErrorDetail(error);
      this.fail(
        "Backtest request failed",
        typeof detail === "string" ? detail : errorMessage(error, "Backtest request failed"),
      );
      this.updateRunningState();
    }
  }

  private async runLean(): Promise<void> {
    const template = this.config.leanValidationTemplate();
    if (template === null) {
      this.fail(
        "LEAN validation template unavailable",
        "Select a strategy with an aligned LEAN validation template.",
      );
      return;
    }
    this.beginRun(
      "Submitting LEAN run…",
      `${this.config.effectiveSymbol()} · ${this.config.startDate()} → ${this.config.endDate()}`,
    );
    try {
      const [startResolution, endResolution] = await Promise.all([
        this.leanSidecar.nextTradingDayOpen(previousIsoDate(this.config.startDate())),
        this.leanSidecar.nextTradingDayOpen(this.config.endDate()),
      ]);
      const configuredSource = this.config.customLeanSource();
      const customSource = configuredSource?.trim() ? configuredSource : null;
      // No registered strategy declares a LEAN twin that takes runtime
      // parameters any more: the bundled template hardcodes its own gates.
      // So a changed parameter cannot reach LEAN, and running anyway would
      // persist an experiment whose results describe different rules than
      // the configuration on screen. Refuse instead — a custom source is the
      // one way to express parameters the template cannot take.
      const changed = this.config.changedParameterNames();
      if (customSource === null && changed.length > 0) {
        this.fail(
          "LEAN cannot run these parameters",
          `The bundled LEAN template hardcodes its own gates, so ${changed.join(", ")} ` +
            "would not reach it. Restore the defaults, or supply a custom LEAN source " +
            "that takes them.",
        );
        this.updateRunningState();
        return;
      }
      const algorithm = customSource === null
        ? { template }
        : { algorithm_source: customSource };
      const leanJobId = await this.jobs.startJob("lean_engine_run", {
        request: {
          run_id: this.composeRunId(),
          requested_engine: this.config.engine(),
          starting_cash: this.config.initialCash(),
          start_ms_utc: startResolution.session_open_ms_utc,
          end_ms_utc: endResolution.session_open_ms_utc,
          data_policy: this.config.dataPolicy(),
          ...algorithm,
        },
      });
      rememberOwnJob(leanJobId);
      this.adoptionSettled = true;
      this.leanJobId.set(leanJobId);
    } catch (error) {
      this.fail(
        "LEAN run request failed",
        error instanceof Error ? error.message : "LEAN run request failed",
      );
      this.updateRunningState();
    }
  }

  private beginRun(headline: string, detail: string): void {
    this.running.set(true);
    this.runError.set(null);
    this.setRunStatus("connecting", headline, detail);
  }

  private fail(headline: string, detail: string): void {
    this.runError.set(detail);
    this.setRunStatus("failed", headline, detail);
  }

  private setRunStatus(
    phase: StrategyLabRunPhase,
    headline: string,
    detail = "",
  ): void {
    this.runPhase.set(phase);
    this.runStatusBanner.set(headline);
    this.runPhaseDetail.set(detail);
  }

  private applyLeanLauncherReport(report: LeanLauncherDiagnosticReport): void {
    const launcher = report.checks.find((check) => check.name === "launcher_healthz");
    const failure = report.checks.find((check) => check.status === "fail");
    if (launcher?.status === "pass" && report.overall_status !== "fail" && !failure) {
      this.leanLauncherStatus.set("ready");
      this.leanLauncherDetail.set(launcher.detail);
      return;
    }
    this.leanLauncherStatus.set("blocked");
    this.leanLauncherDetail.set(
      failure?.fix ??
        failure?.detail ??
        launcher?.fix ??
        launcher?.detail ??
        "LEAN launcher is not reachable.",
    );
  }

  /**
   * Reattach to a job `JobsService` already has in flight when this runner
   * is constructed — e.g. a page reload while a backtest is running.
   * `JobsService.resumeActive()` resolves asynchronously and may finish
   * after this runner is constructed, so this reacts to `activeJobs()`
   * rather than reading it once here. The job this tab started (remembered
   * per tab) is preferred over any other active job of the same type, so a
   * reload never lands on another tab's experiment; a tab with no job of
   * its own adopts an active job only when there is exactly one, so Run
   * stays disabled while the container is busy without guessing between
   * experiments. Adoption happens at most once per runner: once this runner
   * has started or adopted a job, its terminal transition ends the run
   * rather than falling through to some other active job.
   */
  private wireJobAdoptionEffect(): void {
    effect(() => {
      if (this.adoptionSettled || this.engineJobId() !== null || this.leanJobId() !== null) return;
      const active = this.jobs.activeJobs().filter(isStrategyLabJob);
      const own = ownJobId();
      // This tab's own job wins. A tab with no marker at all adopts only an
      // unambiguous single candidate; a marker whose job is no longer active
      // (the tab was closed before the run ended) adopts nothing — never a
      // guess between experiments.
      const job = own === null ? (active.length === 1 ? active[0] : undefined) : active.find((candidate) => candidate.id === own);
      if (job === undefined) return;
      this.adoptionSettled = true;
      if (job.type === "engine_backtest") {
        this.beginRun("Reattaching to backtest…", "");
        this.engineJobId.set(job.id);
      } else {
        this.beginRun("Reattaching to LEAN run…", "");
        this.leanJobId.set(job.id);
      }
    });
  }

  private wireEngineJobEffect(): void {
    effect(() => {
      const id = this.engineJobId();
      if (!id) return;
      const job = this.jobs.job(id);
      if (!job) return;
      const lastLog = job.recentLogs[job.recentLogs.length - 1]?.message ?? "";
      if (job.status === "queued" || job.status === "running") {
        const phase = (job.phase ?? "connecting") as StrategyLabRunPhase;
        const headlines: Record<string, string> = {
          connecting: "Submitting backtest…",
          fetching_data: "Fetching bars from data provider…",
          consolidating_bars: "Consolidating bars to strategy resolution…",
          running_indicators: "Running indicators and strategy logic…",
          aggregating_results: "Aggregating results and statistics…",
          persisting: "Persisting run to history…",
          loading_bars: "Loading bars from cache & Polygon…",
          simulating: "Running engine — consolidating bars and evaluating signals…",
          computing_stats: "Computing statistics & saving study…",
        };
        this.setRunStatus(phase, headlines[phase] ?? `Phase: ${phase}`, lastLog);
        return;
      }
      if (job.status === "failed") {
        this.fail("Backtest failed", job.errorMessage ?? "Backtest failed");
        this.engineJobId.set(null);
        forgetOwnJob();
        this.updateRunningState();
        return;
      }
      if (job.status === "cancelled") {
        this.fail("Backtest cancelled", job.message ?? "");
        this.engineJobId.set(null);
        forgetOwnJob();
        this.updateRunningState();
        return;
      }
      if (job.status === "completed") {
        this.engineJobId.set(null);
        forgetOwnJob();
        void this.handleEngineJobCompleted(id);
      }
    });
  }

  private wireLeanJobEffect(): void {
    effect(() => {
      const id = this.leanJobId();
      if (!id) return;
      const job = this.jobs.job(id);
      if (!job) return;
      const lastLog = job.recentLogs[job.recentLogs.length - 1]?.message ?? "";
      if (job.status === "queued" || job.status === "running") {
        const phase = job.phase ?? "connecting";
        const headlines: Record<string, string> = {
          connecting: "Submitting LEAN run…",
          staging_data: "Staging LEAN data fixtures…",
          launching_sidecar: "Submitting launch request to the LEAN sidecar…",
          sidecar_running: "LEAN container running…",
          parsing_results: "Parsing LEAN output…",
          persisting: "Persisting run to history…",
        };
        const coarse: StrategyLabRunPhase =
          phase === "connecting"
            ? "connecting"
            : phase === "persisting" || phase === "parsing_results"
              ? "computing_stats"
              : "simulating";
        this.setRunStatus(coarse, headlines[phase] ?? `Phase: ${phase}`, lastLog);
        return;
      }
      if (job.status === "failed") {
        this.fail("LEAN run failed", job.errorMessage ?? "LEAN run failed");
        this.leanJobId.set(null);
        forgetOwnJob();
        this.updateRunningState();
        return;
      }
      if (job.status === "cancelled") {
        this.fail("LEAN run cancelled", job.message ?? "");
        this.leanJobId.set(null);
        forgetOwnJob();
        this.updateRunningState();
        return;
      }
      if (job.status === "completed") {
        this.leanJobId.set(null);
        forgetOwnJob();
        void this.handleLeanJobCompleted(id);
      }
    });
  }

  private async handleEngineJobCompleted(jobId: string): Promise<void> {
    try {
      const response = await this.jobs.fetchResult<EngineBacktestResponse>(jobId);
      if (response.error) {
        this.fail("Backtest failed", response.error);
      } else if (response.success) {
        this.setRunStatus(
          "completed",
          `Completed — ${response.total_trades} trade${response.total_trades === 1 ? "" : "s"}, net ${formatCurrency(response.net_profit)}`,
        );
        if (response.study_id != null) {
          this.justProducedRunId.set(response.study_id);
          await this.router.navigate(["/strategy-lab"], {
            queryParams: { run: response.study_id },
            queryParamsHandling: "merge",
          });
        }
        else {
          this.runError.set(
            "Run completed but persistence failed — no report available. The run was not saved to history; check backend logs.",
          );
        }
      }
    } catch (error) {
      this.fail(
        "Failed to fetch backtest result",
        errorMessage(error, "Failed to fetch backtest result"),
      );
    } finally {
      this.updateRunningState();
    }
  }

  private async handleLeanJobCompleted(jobId: string): Promise<void> {
    try {
      const response = await this.jobs.fetchResult<TrustedRunResponse>(jobId);
      if (response.strategy_execution_id !== null) {
        this.setRunStatus(
          "completed",
          "LEAN run finished",
          `Persisted as study #${response.strategy_execution_id}.`,
        );
        this.justProducedRunId.set(response.strategy_execution_id);
        await this.router.navigate(["/strategy-lab"], {
          queryParams: { run: response.strategy_execution_id },
          queryParamsHandling: "merge",
        });
      } else {
        this.runError.set(
          "LEAN run completed but persistence failed — no report available. The run was not saved to history; check backend logs.",
        );
        this.setRunStatus("completed", "LEAN run finished", "Run completed without a persisted study ID.");
      }
    } catch (error) {
      this.fail(
        "LEAN result unavailable",
        error instanceof Error ? error.message : "Failed to fetch LEAN run result",
      );
    } finally {
      this.updateRunningState();
    }
  }

  private updateRunningState(): void {
    this.running.set(this.engineJobId() !== null || this.leanJobId() !== null);
  }
}



function httpErrorDetail(error: unknown): unknown {
  if (!(error instanceof HttpErrorResponse) || typeof error.error !== "object" || error.error === null) {
    return undefined;
  }
  return Object.entries(error.error).find(([key]) => key === "detail")?.[1];
}

function errorMessage(error: unknown, fallback: string): string {
  if (error instanceof HttpErrorResponse && error.message) return error.message;
  return error instanceof Error && error.message ? error.message : fallback;
}

function formatCurrency(value: number): string {
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    maximumFractionDigits: 2,
  }).format(value);
}
