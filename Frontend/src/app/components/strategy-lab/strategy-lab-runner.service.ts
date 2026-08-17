import { HttpErrorResponse } from "@angular/common/http";
import { computed, effect, inject, Injectable, signal } from "@angular/core";
import { Router } from "@angular/router";

import { JobsService } from "../../services/jobs.service";
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
  readonly runPhase = signal<StrategyLabRunPhase>("idle");
  readonly runStatusBanner = signal("");
  readonly runPhaseDetail = signal("");
  readonly runError = signal<string | null>(null);

  constructor() {
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
    return `strategy_lab_${symbol}_${Date.now().toString(36)}`;
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
      this.engineJobId.set(await this.jobs.startJob("engine_backtest", { backtest }));
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
      const strategyParameters = leanStrategyParameters(template, this.config.paramValues());
      this.leanJobId.set(
        await this.jobs.startJob("lean_engine_run", {
          request: {
            run_id: this.composeRunId(),
            requested_engine: this.config.engine(),
            template,
            starting_cash: this.config.initialCash(),
            start_ms_utc: startResolution.session_open_ms_utc,
            end_ms_utc: endResolution.session_open_ms_utc,
            data_policy: this.config.dataPolicy(),
            ...(strategyParameters === undefined ? {} : { strategy_parameters: strategyParameters }),
          },
        }),
      );
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
        this.updateRunningState();
        return;
      }
      if (job.status === "cancelled") {
        this.fail("Backtest cancelled", job.message ?? "");
        this.engineJobId.set(null);
        this.updateRunningState();
        return;
      }
      if (job.status === "completed") {
        this.engineJobId.set(null);
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
        this.updateRunningState();
        return;
      }
      if (job.status === "cancelled") {
        this.fail("LEAN run cancelled", job.message ?? "");
        this.leanJobId.set(null);
        this.updateRunningState();
        return;
      }
      if (job.status === "completed") {
        this.leanJobId.set(null);
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
          await this.router.navigate(["/strategy-lab/runs", response.study_id]);
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
        await this.router.navigate(["/strategy-lab/runs", response.strategy_execution_id]);
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

function leanStrategyParameters(
  template: string,
  values: Record<string, unknown>,
): { gap_bps: number; rsi_min: number; rsi_max: number } | undefined {
  if (template !== "ema_crossover_2_bps") return undefined;
  return {
    gap_bps: requiredFiniteNumber(values, "gap_bps"),
    rsi_min: requiredFiniteNumber(values, "rsi_min"),
    rsi_max: requiredFiniteNumber(values, "rsi_max"),
  };
}

function requiredFiniteNumber(values: Record<string, unknown>, field: string): number {
  const value = values[field];
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${field} must be a finite number`);
  }
  return value;
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
