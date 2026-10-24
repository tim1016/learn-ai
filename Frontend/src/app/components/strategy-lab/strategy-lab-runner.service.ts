import { HttpErrorResponse } from "@angular/common/http";
import { DOCUMENT } from "@angular/common";
import { computed, DestroyRef, effect, inject, Injectable, signal, untracked } from "@angular/core";
import { firstValueFrom } from "rxjs";

import { JobsService, type JobState } from "../../services/jobs.service";
import { LeanSidecarService } from "../../services/lean-sidecar.service";
import { BacktestRunsService } from "../../services/backtest-runs.service";
import type {
  LeanLauncherDiagnosticReport,
  TrustedRunResponse,
} from "../../services/lean-sidecar.types";
import { StrategyLabConfigStore } from "./strategy-lab-config.store";
import {
  previousIsoDate,
  type EngineBacktestResponse,
  type LeanLauncherStatus,
  type StrategyInfo,
  type StrategyLabRunPhase,
} from "./strategy-lab.models";

const COMPATIBILITY_PROFILE = "us-equity-raw-ibkr-v1";

/**
 * The job this browser tab started, kept in `sessionStorage` (per-tab,
 * survives a reload, never shared with other tabs) so a reload reattaches
 * to *this* tab's run even when other backtests are active. The marker names
 * the strategy whose workbench started the job: another strategy's Explore
 * never takes it over. Storage is a convenience, not execution state: when it
 * is unavailable the runner falls back to adopting the one active job of its
 * own strategy.
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
  /** Null only for a marker migrated from the bare-id key, which recorded no type. */
  readonly type: StrategyLabJobType | null;
  /**
   * The registry key of the workbench that started the job. Null for a marker written
   * before the key was recorded: its job's own payload says whose it is while the job is
   * known, and a job that has finished since is nobody's to open.
   */
  readonly strategy: string | null;
}

/** A marker that names its job's type and strategy, so a job that has finished can be read back. */
type NamedOwnJob = OwnJob & { readonly type: StrategyLabJobType; readonly strategy: string };

function isNamed(own: OwnJob): own is NamedOwnJob {
  return own.type !== null && own.strategy !== null;
}

function isStrategyLabJobType(type: string): type is StrategyLabJobType {
  return (STRATEGY_LAB_JOB_TYPES as readonly string[]).includes(type);
}

/** Every LEAN run this workbench submits carries this `run_id` prefix; the
 *  parity companion submits `lean_engine_run` jobs through the same public
 *  route with a `companion-…` id, and those must never be adopted here. */
const STRATEGY_LAB_RUN_ID_PREFIX = "strategy_lab_";

// A save whose write outlasted its wait: re-check history this often, this
// many times, before leaving the still-finishing message standing (#2464).
const STUDY_RECHECK_DELAY_MS = 3_000;
const STUDY_RECHECK_ATTEMPTS = 5;

/** How often an open Strategy Lab asks the registry about jobs other tabs started (#1956). */
const ACTIVE_JOBS_REFRESH_MS = 10_000;

// Run-banner copy per backend phase id, hoisted out of the job effects so it
// is built once rather than on every emission. Engine ids track
// ``ENGINE_BACKTEST_PHASES`` and LEAN ids ``LEAN_ENGINE_RUN_PHASES`` in
// PythonDataService/app/jobs/phases.py; an id with no entry here falls back
// to its raw form, which is the signal that the two have drifted.
const ENGINE_PHASE_HEADLINES: Record<string, string> = {
  connecting: "Submitting backtest…",
  waiting_for_engine: "Waiting for the backtest already running…",
  fetching_data: "Fetching bars from data provider…",
  consolidating_bars: "Consolidating bars to strategy resolution…",
  running_indicators: "Running indicators and strategy logic…",
  aggregating_results: "Aggregating results and statistics…",
  persisting: "Persisting run to history…",
  loading_bars: "Loading bars from cache & Polygon…",
  simulating: "Running engine — consolidating bars and evaluating signals…",
  computing_stats: "Computing statistics & saving study…",
};

const LEAN_PHASE_HEADLINES: Record<string, string> = {
  connecting: "Submitting LEAN run…",
  staging_data: "Staging LEAN data fixtures…",
  launching_sidecar: "Submitting launch request to the LEAN sidecar…",
  sidecar_running: "LEAN container running…",
  parsing_results: "Parsing LEAN output…",
  persisting: "Persisting run to history…",
};

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
    if (raw !== null) return parseOwnJob(JSON.parse(raw));
    // A tab that started its job on the previous frontend still carries the
    // bare-id marker; it is honoured once, under the new key.
    const legacy = sessionStorage.getItem(LEGACY_OWN_JOB_KEY);
    if (legacy === null) return null;
    const migrated: OwnJob = { id: legacy, type: null, strategy: null };
    // The old copy goes only once the new one is stored.
    sessionStorage.setItem(OWN_JOB_KEY, JSON.stringify(migrated));
    sessionStorage.removeItem(LEGACY_OWN_JOB_KEY);
    return migrated;
  } catch {
    return null;
  }
}

/** A stored marker, whichever build wrote it; `null` for anything that is not one. */
function parseOwnJob(value: unknown): OwnJob | null {
  if (typeof value !== "object" || value === null) return null;
  const { id, type, strategy } = value as Record<string, unknown>;
  if (typeof id !== "string") return null;
  if (type !== null && !(typeof type === "string" && isStrategyLabJobType(type))) return null;
  return { id, type, strategy: typeof strategy === "string" ? strategy : null };
}

function readRecord(value: unknown): Readonly<Record<string, unknown>> | null {
  return typeof value === "object" && value !== null ? (value as Record<string, unknown>) : null;
}

/**
 * The registry key a Strategy Lab job belongs to, read back from the payload it
 * was submitted with, or `null` when the payload names none. A Python job names
 * its strategy. A LEAN job names the strategy whose Explore submitted a custom
 * source, or carries a bundled template, which belongs to the strategy that
 * registers it as its LEAN twin; the catalog says which that is.
 */
export function strategyOfJob(job: Pick<JobState, "type" | "parameters">, strategies: readonly StrategyInfo[]): string | null {
  if (job.type === "engine_backtest") {
    const name = readRecord(job.parameters?.["backtest"])?.["strategy_name"];
    return typeof name === "string" && name !== "" ? name : null;
  }
  const request = readRecord(job.parameters?.["request"]);
  if (request === null) return null;
  const owner = request["strategy_key"];
  if (typeof owner === "string" && owner !== "") return owner;
  // A custom source submitted without an owner has none to name.
  if (typeof request["algorithm_source"] === "string") return null;
  const template = request["template"];
  return typeof template === "string" ? strategies.find((candidate) => candidate.lean_twin === template)?.name ?? null : null;
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
  private readonly backtestRuns = inject(BacktestRunsService);
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
  private readonly _adoptedJob = signal<AdoptedJob | null>(null);
  /** The job this tab adopted rather than started, while it is still in
   *  flight, so the workbench can describe its inputs on the rail (#1953).
   *  Once the job ends the rail is the operator's, or the persisted run's. */
  readonly adoptedJob = computed(() => {
    const adopted = this._adoptedJob();
    return adopted !== null && this.jobs.activeJobs().some((job) => job.id === adopted.id) ? adopted : null;
  });
  /** A Strategy Lab job is in flight somewhere — this tab's or another's.
   *  The rail keeps Run disabled on it, so ambiguity between several
   *  experiments never re-enables a third backtest on the busy container. */
  readonly engineBusy = computed(() => this.jobs.activeJobs().some(isStrategyLabJob));
  /**
   * The other strategy whose backtest holds the container, when one is in flight
   * and its payload names it. That job is only ever shown as running under its
   * own strategy: this workbench neither adopts it nor describes its inputs.
   */
  readonly otherStrategyRunning = computed<string | null>(() => {
    const strategies = this.config.strategies();
    for (const job of this.jobs.activeJobs()) {
      if (!isStrategyLabJob(job)) continue;
      const owner = strategyOfJob(job, strategies);
      if (owner !== null && owner !== this.config.pinnedStrategy()) return owner;
    }
    return null;
  });
  readonly runPhase = signal<StrategyLabRunPhase>("idle");
  readonly runStatusBanner = signal("");
  readonly runPhaseDetail = signal("");
  readonly runError = signal<string | null>(null);
  /** The run this runner just persisted, so the workbench can skip a restore
   *  that would clobber the configuration which produced it. */
  readonly justProducedRunId = signal<number | null>(null);
  /**
   * The run this runner persisted and the workbench has not opened yet. The
   * runner never navigates: the workbench opens the run when it is on screen,
   * so a run that finishes while another tool is open does not pull the owner
   * back to the Backtest tab, and is there to open when they return.
   */
  readonly pendingOpenRunId = signal<number | null>(null);
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
    if (this.config.datesMissing()) {
      this.fail("Pick the dates to run over", "A date field in the time window holds no usable date.");
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
      initial_cash: this.config.initialCash(),
      // A paired run fills at the signal bar's close and charges the pinned
      // IBKR equity fee model; sending another fill mode or a flat commission
      // for it would be refused at the request boundary (#2465).
      ...(this.config.engine() === "python"
        ? { fill_mode: this.config.fillMode(), commission_per_order: this.config.commissionPerOrder() }
        : {}),
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
      rememberOwnJob({ id: jobId, type: "engine_backtest", strategy: this.config.pinnedStrategy() });
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
      // A changed parameter the bundled template would ignore cannot run:
      // the persisted experiment would describe different rules than the
      // configuration on screen. Refuse instead — a custom source is the one
      // way to express parameters the template cannot take.
      const ignored = this.config.parametersLeanWouldIgnore();
      if (customSource === null && ignored.length > 0) {
        const one = ignored.length === 1;
        this.fail(
          "LEAN cannot run these parameters",
          `The bundled LEAN template would ignore the ${one ? "value" : "values"} set for ` +
            `${ignored.join(", ")} and run its own. Restore ${one ? "its default" : "their defaults"}, ` +
            `or supply a custom LEAN source that takes ${one ? "it" : "them"}.`,
        );
        this.updateRunningState();
        return;
      }
      const strategyParameters = this.config.leanStrategyParameters();
      const algorithm = customSource === null
        ? { template, ...(strategyParameters === null ? {} : { strategy_parameters: strategyParameters }) }
        : { algorithm_source: customSource, strategy_key: this.config.pinnedStrategy() };
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
      rememberOwnJob({ id: leanJobId, type: "lean_engine_run", strategy: this.config.pinnedStrategy() });
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
   * rather than reading it once here. Only a job of this runner's own
   * strategy is ever taken: a backtest of another strategy keeps the
   * container busy and is shown under its own strategy, never here. The job
   * this tab started for this strategy (remembered per tab) is preferred over
   * any other active job, so a reload never lands on another tab's
   * experiment; a tab with no job of its own adopts an active job of this
   * strategy only when there is exactly one, so Run stays disabled while the
   * container is busy without guessing between experiments. Adoption happens
   * at most once per runner: once this runner has started or adopted a job,
   * its terminal transition ends the run rather than falling through to some
   * other active job.
   */
  private wireJobAdoptionEffect(): void {
    effect(() => {
      if (this.adoptionSettled || this.engineJobId() !== null || this.leanJobId() !== null) return;
      const key = this.config.pinnedStrategy();
      const strategies = this.config.strategies();
      const own = ownJob();
      const known = own === null ? null : (this.jobs.job(own.id) ?? null);
      // A marker is this workbench's when it names the key; one written before the key was
      // recorded is, while its job is known, when that job's payload names it.
      const ownedHere = own !== null && (own.strategy !== null ? own.strategy === key : known !== null && strategyOfJob(known, strategies) === key);
      if (own !== null && ownedHere) {
        // This tab's own job is its own by construction: it is resolved by id,
        // whatever its state, and the job effects then report it as they
        // would have live. One the settled snapshot does not know finished
        // during the reload (#1954); its stored result is read once instead.
        if (known !== null) {
          const type = own.type ?? known.type;
          if (isStrategyLabJobType(type)) this.reattach({ ...known, type });
        } else if (this.jobs.resumed() && isNamed(own)) {
          this.adoptionSettled = true;
          void this.openFinishedOwnJob(own);
        }
        return;
      }
      // A tab with no marker of this strategy's adopts only an unambiguous single
      // candidate of this strategy — never a guess between experiments, and never
      // another strategy's job. Another strategy's marker is left for its workbench.
      const active = this.jobs.activeJobs().filter(isStrategyLabJob);
      // A bundled-template LEAN job names its strategy only through the catalog's LEAN twins.
      if (strategies.length === 0 && active.some((job) => job.type === "lean_engine_run")) {
        untracked(() => void this.config.ensureStrategiesLoaded());
      }
      const candidates = active.filter((job) => strategyOfJob(job, strategies) === key);
      if (candidates.length === 1) this.reattach(candidates[0]);
    });
  }

  /** Track a job as this tab's own; the job effects take it from here. */
  private reattach(job: StrategyLabJob): void {
    this.adoptionSettled = true;
    rememberOwnJob({ id: job.id, type: job.type, strategy: this.config.pinnedStrategy() });
    this._adoptedJob.set({ id: job.id, type: job.type, parameters: job.parameters });
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
        this.setRunStatus(phase, ENGINE_PHASE_HEADLINES[phase] ?? `Phase: ${phase}`, lastLog);
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
        const coarse: StrategyLabRunPhase =
          phase === "connecting"
            ? "connecting"
            : phase === "persisting" || phase === "parsing_results"
              ? "computing_stats"
              : "simulating";
        this.setRunStatus(coarse, LEAN_PHASE_HEADLINES[phase] ?? `Phase: ${phase}`, lastLog);
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

  /** Offer the workbench the study this runner produced; it keeps the configuration that produced it. */
  private openStudy(runId: number): void {
    this.justProducedRunId.set(runId);
    this.pendingOpenRunId.set(runId);
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
  private async openFinishedOwnJob(own: NamedOwnJob): Promise<void> {
    try {
      const response = await this.jobs.fetchResult<EngineBacktestResponse | TrustedRunResponse>(own.id);
      if (!ownJobIs(own)) return;
      forgetOwnJob();
      await (own.type === "lean_engine_run"
        ? this.applyLeanResult(response as TrustedRunResponse)
        : this.applyEngineResult(response as EngineBacktestResponse));
    } catch (error) {
      if (!ownJobIs(own)) return;
      if (error instanceof HttpErrorResponse && error.status === 404) {
        forgetOwnJob();
        return;
      }
      const headline = own.type === "lean_engine_run" ? "LEAN result unavailable" : "Failed to fetch backtest result";
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
    if (response.study_id != null) {
      this.openStudy(response.study_id);
      return Promise.resolve();
    }
    if ((response.save_outcome ?? "failed") === "unknown") return this.waitForStudy();
    this.runError.set(
      "Run completed but persistence failed — no report available. The run was not saved to history; check backend logs.",
    );
    return Promise.resolve();
  }

  /**
   * A save whose write outlasted its wait (#2464): still finishing, never
   * "not saved". The screen says so, re-checks history a few times, and
   * opens the study the moment it lands. In a paired run the LEAN
   * comparison starts only once this row exists, so the note explains why
   * it is not there yet instead of leaving it silently missing.
   */
  private async waitForStudy(): Promise<void> {
    const paired = this.config.engine() === "both";
    this.runError.set(
      "Still saving — the history write outlasted its wait; the run should appear in history shortly. Re-checking…"
      + (paired
        ? " The LEAN comparison starts once the save lands; it is not missing."
        : ""),
    );
    for (let attempt = 0; attempt < STUDY_RECHECK_ATTEMPTS; attempt++) {
      await new Promise((resolve) => setTimeout(resolve, STUDY_RECHECK_DELAY_MS));
      const landed = await this.findJustSavedStudy();
      if (landed !== null) {
        this.runError.set("");
        this.openStudy(landed);
        return;
      }
    }
    this.runError.set(
      "The save is still finishing — the run was not lost and was not marked failed. Check history again in a moment."
      + (paired
        ? " The LEAN comparison will start once the save lands."
        : ""),
    );
  }

  /** The freshest history row matching what this runner just completed, or null. */
  private async findJustSavedStudy(): Promise<number | null> {
    const strategyName = this.config.selectedStrategyName();
    const symbol = this.config.effectiveSymbol();
    if (!strategyName) return null;
    try {
      const runs = await firstValueFrom(this.backtestRuns.list(null, 25, strategyName));
      const matching = runs
        .filter((run) => run.symbol === symbol)
        .sort((a, b) => b.executedAt - a.executedAt);
      return matching[0]?.id ?? null;
    } catch {
      // The re-check is best effort; a failed read just waits for the next.
      return null;
    }
  }

  /** The one reading of a LEAN result: a saved study, or a run that saved nothing. */
  private applyLeanResult(response: TrustedRunResponse): Promise<unknown> {
    if (response.strategy_execution_id !== null) {
      this.setRunStatus("completed", "LEAN run finished", `Persisted as study #${response.strategy_execution_id}.`);
      this.openStudy(response.strategy_execution_id);
      return Promise.resolve();
    }
    if ((response.save_outcome ?? "failed") === "unknown") {
      this.setRunStatus("completed", "LEAN run finished", "The history write is still finishing.");
      return this.waitForStudy();
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
