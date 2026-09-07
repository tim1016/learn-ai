import { HttpErrorResponse } from "@angular/common/http";
import { DOCUMENT } from "@angular/common";
import { computed, DestroyRef, effect, inject, Injectable, signal } from "@angular/core";
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
const OWN_JOB_KEY = "strategyLab.ownJob";
/** The marker's previous shape: a bare job id with no type. Read once and retired. */
const LEGACY_OWN_JOB_KEY = "strategyLab.ownJobId";

const STRATEGY_LAB_JOB_TYPES = ["engine_backtest", "lean_engine_run"] as const;
type StrategyLabJobType = (typeof STRATEGY_LAB_JOB_TYPES)[number];
type StrategyLabJob = JobState & { type: StrategyLabJobType };

/** The job this tab adopted, as it was when adopted: its live state is read from `JobsService.job()`. */
export type AdoptedJob = Pick<StrategyLabJob, "id" | "type" | "parameters">;

/** The job this tab started or adopted: enough to read its result back after a reload. */
interface OwnJob {
  readonly id: string;
  /** Null only for a marker written before the type was recorded; the result's shape then says which engine ran. */
  readonly type: StrategyLabJobType | null;
}

function isStrategyLabJobType(type: string): type is StrategyLabJobType {
  return (STRATEGY_LAB_JOB_TYPES as readonly string[]).includes(type);
}

/** Every LEAN run this workbench submits carries this `run_id` prefix; the
 *  parity companion submits `lean_engine_run` jobs through the same public
 *  route with a `companion-…` id, and those must never be adopted here. */
const STRATEGY_LAB_RUN_ID_PREFIX = "strategy_lab_";

/** How often an open Strategy Lab asks the registry about jobs other tabs started (#1956). */
const ACTIVE_JOBS_REFRESH_MS = 10_000;

function isStrategyLabJob(job: JobState): job is StrategyLabJob {
  if (job.type === "engine_backtest") return true;
  if (job.type !== "lean_engine_run") return false;
  const request = job.parameters?.["request"];
  const runId = typeof request === "object" && request !== null ? (request as Record<string, unknown>)["run_id"] : undefined;
  // Parameters can be missing for old or malformed server state; a job whose
  // owner cannot be established is left alone rather than guessed.
  return typeof runId === "string" && runId.startsWith(STRATEGY_LAB_RUN_ID_PREFIX);
}

function rememberOwnJob(job: OwnJob): void {
  try {
    if (typeof sessionStorage === "undefined") return;
    sessionStorage.setItem(OWN_JOB_KEY, JSON.stringify(job));
  } catch {
    // Denied storage only loses the marker; `wireJobAdoptionEffect` then
    // reattaches solely to an unambiguous single job, never a guess.
  }
}

function ownJob(): OwnJob | null {
  try {
    if (typeof sessionStorage === "undefined") return null;
    const raw = sessionStorage.getItem(OWN_JOB_KEY);
    if (raw !== null) {
      const parsed: unknown = JSON.parse(raw);
      return isOwnJob(parsed) ? parsed : null;
    }
    // A tab that started its job on the previous frontend still carries the
    // bare-id marker; it is honoured once, under the new key.
    const legacy = sessionStorage.getItem(LEGACY_OWN_JOB_KEY);
    if (legacy === null) return null;
    sessionStorage.removeItem(LEGACY_OWN_JOB_KEY);
    const migrated: OwnJob = { id: legacy, type: null };
    sessionStorage.setItem(OWN_JOB_KEY, JSON.stringify(migrated));
    return migrated;
  } catch {
    return null;
  }
}

function isOwnJob(value: unknown): value is OwnJob {
  if (typeof value !== "object" || value === null) return false;
  const { id, type } = value as Record<string, unknown>;
  return typeof id === "string" && (type === null || (typeof type === "string" && isStrategyLabJobType(type)));
}

/** Whether the marker still names `job` — false once a run started meanwhile has replaced it with its own. */
function ownJobIs(job: OwnJob): boolean {
  return ownJob()?.id === job.id;
}

/** Retire the marker — or, given `only`, retire it only while it still names that job, so a run started meanwhile keeps its own. */
function forgetOwnJob(only?: OwnJob): void {
  try {
    if (typeof sessionStorage === "undefined") return;
    if (only !== undefined && !ownJobIs(only)) return;
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
  private readonly destroyRef = inject(DestroyRef);
  private readonly document = inject(DOCUMENT);
  private readonly engineJobId = signal<string | null>(null);
  private readonly leanJobId = signal<string | null>(null);

  readonly config = inject(StrategyLabConfigStore);
  readonly leanLauncherCommand =
    "cd PythonDataService && PYTHONPATH=. ./.venv/bin/python -m uvicorn app.lean_sidecar.launcher.app:app --host 0.0.0.0 --port 8090";
  readonly leanLauncherStatus = signal<LeanLauncherStatus>("unknown");
  readonly leanLauncherDetail = signal("");
  readonly running = signal(false);
  /** The job this tab adopted rather than started, set once per runner, so
   *  the workbench can describe its inputs on the rail while it runs (#1953). */
  readonly adoptedJob = signal<AdoptedJob | null>(null);
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
    this.wireActiveJobsRefresh();
  }

  /**
   * A tab open before another tab starts a backtest would otherwise never
   * learn the container is busy (#1956): the registry is read once at load,
   * after which only this tab's own jobs and their streams reach it. The lab
   * reads it again on open, whenever the tab becomes visible, and
   * periodically while visible, so a job started anywhere disables Run here
   * too. A read that fails is recorded by JobsService and shown by the rail.
   */
  private wireActiveJobsRefresh(): void {
    const refreshWhenVisible = (): void => {
      if (this.document.visibilityState === "visible") void this.jobs.refreshActive();
    };
    refreshWhenVisible();
    const timer = setInterval(refreshWhenVisible, ACTIVE_JOBS_REFRESH_MS);
    this.document.addEventListener("visibilitychange", refreshWhenVisible);
    this.destroyRef.onDestroy(() => {
      clearInterval(timer);
      this.document.removeEventListener("visibilitychange", refreshWhenVisible);
    });
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
      rememberOwnJob({ id: jobId, type: "engine_backtest" });
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
      rememberOwnJob({ id: leanJobId, type: "lean_engine_run" });
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
      const own = ownJob();
      if (own !== null) {
        // This tab's own job is its own by construction: it is resolved by id,
        // whatever its state, and the job effects then report it as they
        // would have live. One the settled snapshot does not know finished
        // during the reload (#1954); its stored result is read once instead.
        const known = this.jobs.job(own.id);
        if (known !== undefined && known !== null) {
          const type = own.type ?? known.type;
          if (isStrategyLabJobType(type)) this.reattach({ ...known, type });
        } else if (this.jobs.resumed()) {
          this.adoptionSettled = true;
          void this.openFinishedOwnJob(own);
        }
        return;
      }
      // A tab with no marker adopts only an unambiguous single candidate —
      // never a guess between experiments.
      const active = this.jobs.activeJobs().filter(isStrategyLabJob);
      if (active.length === 1) this.reattach(active[0]);
    });
  }

  /** Track a job as this tab's own; the job effects take it from here. */
  private reattach(job: StrategyLabJob): void {
    this.adoptionSettled = true;
    rememberOwnJob({ id: job.id, type: job.type });
    this.adoptedJob.set({ id: job.id, type: job.type, parameters: job.parameters });
    if (job.type === "engine_backtest") {
      this.beginRun("Reattaching to backtest…", "");
      this.engineJobId.set(job.id);
    } else {
      this.beginRun("Reattaching to LEAN run…", "");
      this.leanJobId.set(job.id);
    }
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

  /** Navigate to a study this runner produced; the workbench keeps the configuration that produced it. */
  private openStudy(runId: number): Promise<boolean> {
    this.justProducedRunId.set(runId);
    return this.router.navigate(["/strategy-lab"], { queryParams: { run: runId }, queryParamsHandling: "merge" });
  }

  /**
   * The outcome of a remembered job that finished while this tab was
   * reloading, read from its stored result and interpreted exactly as the
   * live path interprets it. A 404 is the one expected miss — the job
   * failed, was cancelled, or its result expired — and retires the marker
   * with nothing to report, since the reload already showed no run in
   * flight. Any other failure is reported and keeps the marker, so a later
   * reload can try again once the backend answers. A run started while the
   * read was in flight has replaced the marker with its own and owns the
   * tab's outcome from then on: whatever the read returns is discarded, so
   * it can neither navigate away from nor restate the newer run.
   */
  private async openFinishedOwnJob(own: OwnJob): Promise<void> {
    try {
      const response = await this.jobs.fetchResult<EngineBacktestResponse | TrustedRunResponse>(own.id);
      if (!ownJobIs(own)) return;
      forgetOwnJob();
      // A marker from before the type was recorded is told apart by the result's shape.
      const isLean = own.type === "lean_engine_run" || (own.type === null && "strategy_execution_id" in response);
      await (isLean ? this.applyLeanResult(response as TrustedRunResponse) : this.applyEngineResult(response as EngineBacktestResponse));
    } catch (error) {
      if (!ownJobIs(own)) return;
      if (error instanceof HttpErrorResponse && error.status === 404) {
        forgetOwnJob();
        return;
      }
      const headline =
        own.type === "lean_engine_run" ? "LEAN result unavailable"
        : own.type === "engine_backtest" ? "Failed to fetch backtest result"
        : "Result of the run in flight unavailable";
      this.fail(headline, errorMessage(error, headline));
    } finally {
      this.updateRunningState();
    }
  }

  private async handleEngineJobCompleted(jobId: string): Promise<void> {
    try {
      await this.applyEngineResult(await this.jobs.fetchResult<EngineBacktestResponse>(jobId));
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
      await this.applyLeanResult(await this.jobs.fetchResult<TrustedRunResponse>(jobId));
    } catch (error) {
      this.fail(
        "LEAN result unavailable",
        error instanceof Error ? error.message : "Failed to fetch LEAN run result",
      );
    } finally {
      this.updateRunningState();
    }
  }

  /** The one reading of an engine result: a reported failure, a saved study, or a run that saved nothing. */
  private applyEngineResult(response: EngineBacktestResponse): Promise<unknown> {
    if (response.error) {
      this.fail("Backtest failed", response.error);
      return Promise.resolve();
    }
    if (!response.success) return Promise.resolve();
    this.setRunStatus(
      "completed",
      `Completed — ${response.total_trades} trade${response.total_trades === 1 ? "" : "s"}, net ${formatCurrency(response.net_profit)}`,
    );
    if (response.study_id != null) return this.openStudy(response.study_id);
    this.runError.set(
      "Run completed but persistence failed — no report available. The run was not saved to history; check backend logs.",
    );
    return Promise.resolve();
  }

  /** The one reading of a LEAN result: a saved study, or a run that saved nothing. */
  private applyLeanResult(response: TrustedRunResponse): Promise<unknown> {
    if (response.strategy_execution_id !== null) {
      this.setRunStatus("completed", "LEAN run finished", `Persisted as study #${response.strategy_execution_id}.`);
      return this.openStudy(response.strategy_execution_id);
    }
    this.runError.set(
      "LEAN run completed but persistence failed — no report available. The run was not saved to history; check backend logs.",
    );
    this.setRunStatus("completed", "LEAN run finished", "Run completed without a persisted study ID.");
    return Promise.resolve();
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
