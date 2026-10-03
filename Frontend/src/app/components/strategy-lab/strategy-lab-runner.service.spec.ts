import { HttpErrorResponse, provideHttpClient } from "@angular/common/http";
import { computed, signal } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { ActivatedRoute, Router, convertToParamMap } from "@angular/router";
import { of } from "rxjs";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { JobsService } from "../../services/jobs.service";
import type { JobState, JobStatus } from "../../services/jobs.service";
import { LeanSidecarService } from "../../services/lean-sidecar.service";
import { BacktestRunsService } from "../../services/backtest-runs.service";
import { StrategyLabConfigStore } from "./strategy-lab-config.store";
import { StrategyLabRunner } from "./strategy-lab-runner.service";
import { inputsFromBacktestJob, type StrategyInfo } from "./strategy-lab.models";

const TERMINAL_JOB_STATUSES: JobStatus[] = ["completed", "failed", "cancelled"];

/** Mirrors `JobsService.job()`/`activeJobs()` well enough to test a runner
 *  reacting to a job JobsService discovered on its own (e.g. `resumeActive()`
 *  finishing after the runner is constructed), instead of one it started. */
function makeJobState(overrides: Partial<JobState> & Pick<JobState, "id" | "type" | "status">): JobState {
  return { recentLogs: [], logSeq: 0, ...overrides };
}

const STRATEGY = {
  name: "ema_crossover_signal",
  display_name: "EMA crossover",
  description: "EMA validation",
  params_schema: { properties: { symbol: { type: "string", default: "SPY" } } },
  supported_resolutions: ["minute"],
  lean_twin: "ema_crossover_signal",
  strategy_bars: { timespan: "minute", multiplier: 15, parameter: null },
} satisfies StrategyInfo;

const PARAMETERIZED_STRATEGY = {
  ...STRATEGY,
  name: "ema_crossover_2_bps",
  display_name: "EMA Crossover 2 bps",
  lean_twin: "ema_crossover_2_bps",
  params_schema: {
    properties: {
      symbol: { type: "string", default: "SPY" },
      gap_bps: { type: "number", default: 2 },
      rsi_min: { type: "number", default: 50 },
      rsi_max: { type: "number", default: 70 },
    },
  },
} satisfies StrategyInfo;

describe("StrategyLab configuration and runner", () => {
  let config: StrategyLabConfigStore;
  let runner: StrategyLabRunner;
  let startJob: ReturnType<typeof vi.fn>;
  let fetchResult: ReturnType<typeof vi.fn>;
  let listRuns: ReturnType<typeof vi.fn>;
  let refreshActive: ReturnType<typeof vi.fn>;
  let nextTradingDayOpen: ReturnType<typeof vi.fn>;
  let diagnose: ReturnType<typeof vi.fn>;
  let navigate: ReturnType<typeof vi.fn>;
  let jobsById: ReturnType<typeof signal<Map<string, JobState>>>;
  let resumed: ReturnType<typeof signal<boolean>>;

  /** Adds/replaces a job the way `JobsService` itself would — used to
   *  simulate a job `resumeActive()` discovered rather than one this
   *  runner started. */
  function putJob(job: JobState): void {
    jobsById.update((map) => {
      const next = new Map(map);
      next.set(job.id, job);
      return next;
    });
  }

  function configureLab(): void {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        StrategyLabConfigStore,
        StrategyLabRunner,
        { provide: ActivatedRoute, useValue: { queryParamMap: of(convertToParamMap({})) } },
        { provide: Router, useValue: { navigate } },
        {
          provide: JobsService,
          useValue: {
            jobs: computed(() => Array.from(jobsById().values())),
            activeJobs: computed(() =>
              Array.from(jobsById().values()).filter((job) => !TERMINAL_JOB_STATUSES.includes(job.status)),
            ),
            job: (id: string) => jobsById().get(id) ?? null,
            resumed,
            startJob,
            fetchResult,
            refreshActive,
          },
        },
        {
          provide: LeanSidecarService,
          useValue: {
            diagnose,
            nextTradingDayOpen,
          },
        },
        {
          provide: BacktestRunsService,
          useValue: { list: listRuns },
        },
      ],
    });
  }

  beforeEach(() => {
    sessionStorage.clear();
    startJob = vi.fn(async () => "job-1");
    fetchResult = vi.fn();
    listRuns = vi.fn(() => of([]));
    refreshActive = vi.fn(async () => undefined);
    navigate = vi.fn(async () => true);
    jobsById = signal(new Map<string, JobState>());
    resumed = signal(false);
    diagnose = vi.fn(async () => ({
      overall_status: "pass",
      checks: [{ name: "launcher_healthz", status: "pass", detail: "ready" }],
    }));
    nextTradingDayOpen = vi.fn(async (date: string) => ({
      session_open_ms_utc: date === "2026-01-04" ? 1_767_624_600_000 : 1_768_000_000_000,
    }));
    configureLab();
    config = TestBed.inject(StrategyLabConfigStore);
    runner = TestBed.inject(StrategyLabRunner);
    config.strategies.set([STRATEGY]);
    config.selectStrategy(STRATEGY.name);
  });

  it("submits one Python anchor for Both mode with the exact compatibility policy", async () => {
    config.engine.set("both");

    await runner.run();

    expect(startJob).toHaveBeenCalledOnce();
    expect(startJob).toHaveBeenCalledWith(
      "engine_backtest",
      expect.objectContaining({
        backtest: expect.objectContaining({
          requested_engine: "both",
          compatibility_profile: "us-equity-raw-ibkr-v1",
          data_policy: expect.objectContaining({ adjusted: false, symbol: "SPY" }),
        }),
      }),
    );
    // A paired run charges the pinned IBKR fee model; the request boundary
    // refuses an explicit flat commission (#2465), so none is sent.
    const [, payload] = startJob.mock.calls[0] as [string, { backtest: Record<string, unknown> }];
    expect(payload.backtest).not.toHaveProperty("commission_per_order");
    // Nor another fill mode: the profile fills at the signal bar's close.
    expect(payload.backtest).not.toHaveProperty("fill_mode");
  });

  it("submits a Python run that fills at the decision minute's open when the rail is left as it loads (#2599)", async () => {
    await runner.run();

    expect(startJob).toHaveBeenCalledOnce();
    const [, payload] = startJob.mock.calls[0] as [string, { backtest: Record<string, unknown> }];
    expect(payload.backtest["requested_engine"]).toBe("python");
    expect(payload.backtest["fill_mode"]).toBe("decision_minute_open");
  });

  it("fills a Python run at the decision minute's open by default, and names the signal bar's close for LEAN whatever the rail holds (#2599)", () => {
    expect(config.runFillMode()).toBe("decision_minute_open");

    config.engine.set("both");
    expect(config.runFillMode()).toBe("signal_bar_close");
    config.engine.set("lean");
    expect(config.runFillMode()).toBe("signal_bar_close");
  });

  it("keeps data-policy cadence and adjustment choices in the configuration store", () => {
    config.engine.set("python");
    expect(config.dataPolicy()).toEqual(expect.objectContaining({
      adjusted: true,
      input_bars: { timespan: "minute", multiplier: 1 },
      strategy_bars: { timespan: "minute", multiplier: 15 },
    }));
  });

  it("drops undeclared fields from a restored data policy before it reaches the engine", async () => {
    const restoredPolicy = {
      ...config.dataPolicy(),
      extra_field: "persisted-only",
      input_bars: {
        ...config.dataPolicy().input_bars,
        extra_field: "persisted-only",
      },
      strategy_bars: {
        ...config.dataPolicy().strategy_bars,
        extra_field: "persisted-only",
      },
    };
    config.restoreDataPolicy(restoredPolicy);

    await runner.run();

    const submittedPayload = startJob.mock.calls[0]?.[1];
    expect(submittedPayload).toEqual(expect.objectContaining({
      backtest: expect.objectContaining({
        data_policy: expect.objectContaining({
          input_bars: { timespan: "minute", multiplier: 1 },
          strategy_bars: { timespan: "minute", multiplier: 15 },
        }),
      }),
    }));
    expect(JSON.stringify(submittedPayload)).not.toContain("extra_field");
  });

  it("selects a runnable strategy after an ordinary engine change", () => {
    const pythonOnly = { ...STRATEGY, name: "python_only", lean_twin: null };
    config.strategies.set([pythonOnly, STRATEGY]);
    config.selectStrategy(pythonOnly.name);

    config.changeEngine("lean");
    TestBed.tick();

    expect(config.selectedStrategyName()).toBe(STRATEGY.name);
  });

  it.each(["lean", "python"] as const)("releases a restored raw Both snapshot when switching to %s", async (engine) => {
    config.engine.set("both");
    config.range.update((range) => ({ ...range, autoFetch: false }));
    config.restoreDataPolicy({
      ...config.dataPolicy(),
      provider_kind: "fixture",
      fixture_id: "bar-store-v1-raw-snapshot",
      fixture_sha256: "raw-snapshot-hash",
    });

    config.changeEngine(engine);
    await runner.run();

    const [, payload] = startJob.mock.calls[0] as [string, {
      request?: { data_policy: unknown };
      backtest?: { data_policy: unknown };
    }];
    expect((payload.request ?? payload.backtest)?.data_policy).toEqual(expect.objectContaining({
      adjusted: true,
      provider_kind: "live",
      fixture_id: null,
      fixture_sha256: null,
    }));
    expect(config.autoFetch()).toBe(true);
    if (engine === "python") {
      expect(payload.backtest).toEqual(expect.objectContaining({ auto_fetch: true }));
    }
  });

  it("retains the frozen receipt when rerunning Both without changing its adjustment mode", async () => {
    config.engine.set("both");
    const policy = {
      ...config.dataPolicy(),
      provider_kind: "fixture" as const,
      fixture_id: "bar-store-v1-raw-snapshot",
      fixture_sha256: "raw-snapshot-hash",
    };
    config.restoreDataPolicy(policy);

    config.changeEngine("both");
    await runner.run();

    expect(startJob).toHaveBeenCalledWith("engine_backtest", expect.objectContaining({
      backtest: expect.objectContaining({ data_policy: policy }),
    }));
  });

  it("resolves configurable strategy cadence from registry metadata instead of strategy names", () => {
    const strategy = {
      ...STRATEGY,
      name: "sma_crossover",
      params_schema: {
        properties: {
          symbol: { type: "string", default: "SPY" },
          resolution_minutes: { type: "integer", default: 15 },
        },
      },
      strategy_bars: { timespan: "minute" as const, multiplier: 15, parameter: "resolution_minutes" },
    };
    config.strategies.set([strategy]);
    config.selectStrategy(strategy.name);
    config.updateParameter("resolution_minutes", "30", "integer");

    expect(config.dataPolicy().strategy_bars).toEqual({ timespan: "minute", multiplier: 30 });
  });

  it("delegates LEAN session anchors to the canonical calendar service", async () => {
    config.engine.set("lean");
    config.range.update((range) => ({ ...range, from: "2026-01-05" }));

    await runner.run();

    expect(nextTradingDayOpen).toHaveBeenCalledWith("2026-01-04");
    expect(nextTradingDayOpen).toHaveBeenCalledWith(config.endDate());
    expect(startJob).toHaveBeenCalledWith(
      "lean_engine_run",
      expect.objectContaining({
        request: expect.objectContaining({ start_ms_utc: 1_767_624_600_000 }),
      }),
    );
  });

  it("refuses a LEAN run whose changed parameters the template cannot take", async () => {
    // The parameterized LEAN twin went away with the `ema_crossover_2_bps`
    // registration in the signal/asset decoupling sweep, so the bundled
    // template hardcodes its own gates. Running anyway would persist an
    // experiment whose results describe different rules than the screen
    // (#1865 review) -- a silent misattribution, so refuse instead.
    config.strategies.set([PARAMETERIZED_STRATEGY]);
    config.selectStrategy(PARAMETERIZED_STRATEGY.name);
    config.updateParameter("gap_bps", "4", "number");
    config.engine.set("lean");

    await runner.run();

    expect(startJob).not.toHaveBeenCalled();
    expect(runner.runError()).toContain("gap_bps");
  });

  it("still sends the template when every parameter is left at its default", async () => {
    // Selecting a strategy materializes all defaults into paramValues, so a
    // presence test here would refuse every unedited run.
    config.strategies.set([PARAMETERIZED_STRATEGY]);
    config.selectStrategy(PARAMETERIZED_STRATEGY.name);
    config.engine.set("lean");

    await runner.run();

    const request = startJob.mock.calls[0]?.[1]?.request;
    expect(request).toHaveProperty("template");
    expect(request).not.toHaveProperty("strategy_parameters");
  });

  it("runs browser-edited QCAlgorithm source without requiring template parameters", async () => {
    const customSource = "from AlgorithmImports import *\nclass MyAlgorithm(QCAlgorithm):\n    pass\n\n";
    config.engine.set("lean");
    config.customLeanSource.set(customSource);

    await runner.run();

    const request = startJob.mock.calls[0]?.[1]?.request;
    expect(request).toEqual(expect.objectContaining({ algorithm_source: customSource }));
    expect(request).not.toHaveProperty("template");
    expect(request).not.toHaveProperty("strategy_parameters");
  });

  it("rejects an empty custom source before probing the launcher", async () => {
    config.engine.set("lean");
    config.customLeanSource.set("  \n");

    await runner.run();

    expect(runner.runError()).toBe("Load or enter a QCAlgorithm before running custom source.");
    expect(diagnose).not.toHaveBeenCalled();
    expect(startJob).not.toHaveBeenCalled();
  });

  describe("learning about jobs other tabs start (#1956)", () => {
    let lab: StrategyLabRunner;
    let visibility: ReturnType<typeof vi.spyOn>;

    beforeEach(() => {
      // Timers are faked before this runner exists, so its interval is the faked one.
      vi.useFakeTimers();
      TestBed.resetTestingModule();
      configureLab();
      visibility = vi.spyOn(document, "visibilityState", "get").mockReturnValue("visible");
      refreshActive.mockClear();
      lab = TestBed.inject(StrategyLabRunner);
    });

    afterEach(() => {
      visibility.mockRestore();
      vi.useRealTimers();
    });

    it("reads the registry on open and again periodically while the tab is visible", async () => {
      expect(lab).toBeDefined();
      expect(refreshActive).toHaveBeenCalledTimes(1);

      await vi.advanceTimersByTimeAsync(10_000);
      expect(refreshActive).toHaveBeenCalledTimes(2);

      visibility.mockReturnValue("hidden");
      await vi.advanceTimersByTimeAsync(20_000);
      expect(refreshActive).toHaveBeenCalledTimes(2);

      visibility.mockReturnValue("visible");
      document.dispatchEvent(new Event("visibilitychange"));
      expect(refreshActive).toHaveBeenCalledTimes(3);
    });

    it("stops reading once the lab closes", async () => {
      expect(lab).toBeDefined();
      TestBed.resetTestingModule();
      refreshActive.mockClear();

      await vi.advanceTimersByTimeAsync(30_000);
      document.dispatchEvent(new Event("visibilitychange"));
      expect(refreshActive).not.toHaveBeenCalled();
    });
  });

  describe("resuming a job JobsService already had in flight (e.g. after a page reload)", () => {
    it("adopts an active engine_backtest job discovered after construction and reports running", () => {
      expect(runner.running()).toBe(false);

      // JobsService's own resumeActive() resolves asynchronously, after this
      // runner was already constructed — so the job only appears once the
      // signal updates, not at construction time.
      putJob(makeJobState({ id: "resumed-1", type: "engine_backtest", status: "running" }));
      TestBed.tick();

      expect(runner.running()).toBe(true);
    });

    it("exposes the job it adopted, and never one this tab started itself", async () => {
      // #1953: the workbench describes an adopted job's inputs on the rail;
      // a run this tab started already has its inputs on screen.
      await runner.run();
      expect(runner.adoptedJob()).toBeNull();

      const reloaded = TestBed.runInInjectionContext(() => new StrategyLabRunner());
      putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "running", parameters: { backtest: { strategy_name: "ema_crossover_signal" } } }));
      TestBed.tick();

      expect(reloaded.adoptedJob()?.id).toBe("job-1");
      expect(reloaded.adoptedJob()?.parameters).toEqual({ backtest: { strategy_name: "ema_crossover_signal" } });

      // Once the job ends the rail is no longer the adopted job's to describe.
      putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "completed" }));
      expect(reloaded.adoptedJob()).toBeNull();
    });

    it("reads the payload it submits back into the same inputs, so nothing added to the run is lost on the rail", async () => {
      config.engine.set("both");
      config.range.update((range) => ({ ...range, from: "2026-01-05", to: "2026-02-05" }));
      config.initialCash.set(75_000);
      config.commissionPerOrder.set(0.35);
      config.fillMode.set("next_bar_open");
      await runner.run();

      const [type, payload] = startJob.mock.calls[0] as [string, Record<string, unknown>];
      expect(type).toBe("engine_backtest");
      // The symbol travels in the data policy, and the policy's bars come
      // back as the range's multiplier and session. A paired run carries no
      // commission and no fill mode to restore — the rail normalizes the
      // absent values to 0 and the signal bar's close for its hidden
      // controls, and the pinned profile governs (#2465).
      const symbol = config.effectiveSymbol();
      expect(inputsFromBacktestJob(payload, config.range())).toEqual({
        strategyName: STRATEGY.name,
        engine: "both",
        range: { ...config.range(), symbol, multiplier: 1, session: "rth" },
        parameters: { ...config.paramValues(), symbol },
        fillMode: "signal_bar_close",
        initialCash: 75_000,
        commissionPerOrder: 0,
        dataPolicy: config.dataPolicy(),
      });
    });

    it("adopts an active lean_engine_run job discovered after construction and reports running", () => {
      putJob(makeJobState({ id: "resumed-lean-1", type: "lean_engine_run", status: "running", parameters: { request: { run_id: "strategy_lab_spy_x1" } } }));
      TestBed.tick();

      expect(runner.running()).toBe(true);
    });

    it("does not adopt a second job while one is already tracked", async () => {
      config.engine.set("both");
      await runner.run();
      expect(startJob).toHaveBeenCalledOnce();

      // A genuinely active second job must not displace the tracked job-1 …
      fetchResult.mockResolvedValue({ success: true, study_id: 999, total_trades: 1, net_profit: 1 });
      putJob(makeJobState({ id: "resumed-2", type: "engine_backtest", status: "running" }));
      TestBed.tick();
      expect(runner.running()).toBe(true);

      // … so when that second job completes, its study is not the one this
      // runner navigates to.
      putJob(makeJobState({ id: "resumed-2", type: "engine_backtest", status: "completed" }));
      TestBed.tick();
      await Promise.resolve();
      expect(navigate).not.toHaveBeenCalled();
    });

    it("says a save that is still finishing is finishing, re-checks history, and opens the study when it lands (#2464)", async () => {
      // Fake timers from the start: the re-check waits are real seconds, and
      // they must be owned by the clock this test advances.
      vi.useFakeTimers();
      try {
        config.engine.set("python");
        await runner.run();
        fetchResult.mockResolvedValue({
          success: true,
          study_id: null,
          save_outcome: "unknown",
          total_trades: 2,
          net_profit: 5,
        });
        const landed = {
          id: 512,
          strategyName: STRATEGY.name,
          symbol: "SPY",
          executedAt: 1,
        };
        let reads = 0;
        listRuns.mockImplementation(() => {
          reads += 1;
          return of(reads < 2 ? [] : [landed]);
        });
        putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "completed" }));
        TestBed.tick();
        await vi.waitFor(() => {
          expect(runner.runError()).toContain("Still saving");
        });
        expect(runner.runError()).not.toContain("not saved");

        // The re-check finds the landed row on its second look: 3 s of wait,
        // an empty read, another 3 s, the row.
        // One advance through every re-check wait: the first read is empty,
        // the second lands, and the study opens — however the timers
        // interleave with the waits.
        await vi.advanceTimersByTimeAsync(30_000);
        await vi.waitFor(() => {
          expect(navigate).toHaveBeenCalledWith(
            expect.anything(),
            expect.objectContaining({ queryParams: expect.objectContaining({ run: 512 }) }),
          );
        });
        expect(runner.runError()).toBe("");
      } finally {
        vi.useRealTimers();
      }
    });

    it("keeps the confirmed-failure message for a save that failed", async () => {
      config.engine.set("python");
      await runner.run();
      putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "completed" }));
      fetchResult.mockResolvedValue({
        success: true,
        study_id: null,
        save_outcome: "failed",
        total_trades: 0,
        net_profit: 0,
      });
      TestBed.tick();
      await vi.waitFor(() => {
        expect(runner.runError()).toContain("not saved to history");
      });
    });

    it("explains why a paired run's LEAN comparison is missing while its save finishes (#2464)", async () => {
      vi.useFakeTimers();
      try {
        config.engine.set("both");
        await runner.run();
        fetchResult.mockResolvedValue({
          success: true,
          study_id: null,
          save_outcome: "unknown",
          total_trades: 1,
          net_profit: 1,
        });
        listRuns.mockImplementation(() => of([]));
        putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "completed" }));
        TestBed.tick();
        await vi.waitFor(() => {
          expect(runner.runError()).toContain("LEAN comparison starts once the save lands");
        });
        // Exhaust the re-checks: the message stays honest — still finishing,
        // never "not saved".
        await vi.advanceTimersByTimeAsync(16_000);
        expect(runner.runError()).toContain("still finishing");
        expect(runner.runError()).not.toContain("not saved");
      } finally {
        vi.useRealTimers();
      }
    });

    it("reattaches to the job this tab started, not another tab's active backtest", async () => {
      await runner.run();
      expect(startJob).toHaveBeenCalledWith("engine_backtest", expect.anything());

      // Simulate the reload: a fresh runner with no job of its own, while two
      // engine jobs are active and the other tab's job is listed first.
      const reloaded = TestBed.runInInjectionContext(() => new StrategyLabRunner());
      putJob(makeJobState({ id: "other-tab", type: "engine_backtest", status: "running" }));
      putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "running" }));
      TestBed.tick();
      expect(reloaded.running()).toBe(true);

      fetchResult.mockResolvedValue({ success: true, study_id: 777, total_trades: 1, net_profit: 1 });
      putJob(makeJobState({ id: "other-tab", type: "engine_backtest", status: "completed" }));
      TestBed.tick();
      await Promise.resolve();
      expect(navigate).not.toHaveBeenCalled();

      fetchResult.mockResolvedValue({ success: true, study_id: 224, total_trades: 2, net_profit: 150 });
      putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "completed" }));
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();
      expect(navigate).toHaveBeenCalledWith(
        ["/strategy-lab"],
        expect.objectContaining({ queryParams: { run: 224 } }),
      );
    });

    it("opens the study of a remembered job that finished during the reload", async () => {
      // #1954: the tab's job completed between the reload and the active-jobs
      // snapshot, so it never appears in activeJobs() and no terminal event
      // arrives; the saved study was reachable only through History. The
      // runner from beforeEach has not ticked yet, so it stands in for the
      // freshly loaded tab (a second instance would share this tab's marker).
      sessionStorage.setItem("strategyLab.ownJob", JSON.stringify({ id: "job-9", type: "engine_backtest" }));
      fetchResult.mockResolvedValue({ success: true, study_id: 321, total_trades: 4, net_profit: 12 });
      TestBed.tick();

      // Until the snapshot settles, absence means "not yet known": nothing is read.
      expect(fetchResult).not.toHaveBeenCalled();

      resumed.set(true);
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();

      expect(fetchResult).toHaveBeenCalledWith("job-9");
      expect(navigate).toHaveBeenCalledWith(["/strategy-lab"], expect.objectContaining({ queryParams: { run: 321 } }));
      expect(runner.running()).toBe(false);
      expect(sessionStorage.getItem("strategyLab.ownJob")).toBeNull();
    });

    it("retires the marker and reports nothing when the remembered job's result is gone (404)", async () => {
      sessionStorage.setItem("strategyLab.ownJob", JSON.stringify({ id: "job-9", type: "engine_backtest" }));
      fetchResult.mockRejectedValue(new HttpErrorResponse({ status: 404, statusText: "Not Found", error: { error: "result not found or expired" } }));
      resumed.set(true);
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();

      expect(fetchResult).toHaveBeenCalledWith("job-9");
      expect(navigate).not.toHaveBeenCalled();
      expect(runner.runError()).toBeNull();
      expect(sessionStorage.getItem("strategyLab.ownJob")).toBeNull();
    });

    it("reports any other read failure and keeps the marker for a later reload", async () => {
      sessionStorage.setItem("strategyLab.ownJob", JSON.stringify({ id: "job-9", type: "engine_backtest" }));
      fetchResult.mockRejectedValue(new HttpErrorResponse({ status: 503, statusText: "Service Unavailable" }));
      resumed.set(true);
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();

      expect(navigate).not.toHaveBeenCalled();
      expect(runner.runError()).not.toBeNull();
      expect(sessionStorage.getItem("strategyLab.ownJob")).not.toBeNull();
    });

    it("honours the previous frontend's bare-id marker once, telling the engine apart by the result's shape", async () => {
      sessionStorage.setItem("strategyLab.ownJobId", "job-old");
      fetchResult.mockResolvedValue({ strategy_execution_id: 808, exit_code: 0 });
      resumed.set(true);
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();

      expect(fetchResult).toHaveBeenCalledWith("job-old");
      expect(navigate).toHaveBeenCalledWith(["/strategy-lab"], expect.objectContaining({ queryParams: { run: 808 } }));
      expect(sessionStorage.getItem("strategyLab.ownJobId")).toBeNull();
      expect(sessionStorage.getItem("strategyLab.ownJob")).toBeNull();
    });

    it("names LEAN when a remembered LEAN result cannot be read", async () => {
      sessionStorage.setItem("strategyLab.ownJob", JSON.stringify({ id: "lean-9", type: "lean_engine_run" }));
      fetchResult.mockRejectedValue(new HttpErrorResponse({ status: 503, statusText: "Service Unavailable" }));
      resumed.set(true);
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();

      expect(runner.runStatusBanner()).toBe("LEAN result unavailable");
    });

    it("uses an engine-neutral headline when a migrated marker's result cannot be read", async () => {
      sessionStorage.setItem("strategyLab.ownJobId", "job-old");
      fetchResult.mockRejectedValue(new HttpErrorResponse({ status: 503, statusText: "Service Unavailable" }));
      resumed.set(true);
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();

      expect(runner.runStatusBanner()).toBe("Result of the run in flight unavailable");
      expect(sessionStorage.getItem("strategyLab.ownJob")).not.toBeNull();
    });

    it("discards the remembered result when a run started while it was still being read", async () => {
      sessionStorage.setItem("strategyLab.ownJob", JSON.stringify({ id: "job-9", type: "engine_backtest" }));
      let settle: (value: unknown) => void = () => undefined;
      fetchResult.mockReturnValueOnce(new Promise((resolve) => { settle = resolve; }));
      resumed.set(true);
      TestBed.tick();
      await Promise.resolve();

      // The operator starts a new run before the old result arrives.
      await runner.run();
      expect(JSON.parse(sessionStorage.getItem("strategyLab.ownJob") ?? "{}").id).toBe("job-1");
      const phase = runner.runPhase();
      const banner = runner.runStatusBanner();

      settle({ success: true, study_id: 321, total_trades: 4, net_profit: 12 });
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();

      // The new run keeps its marker and its status; the old study is not opened over it.
      expect(JSON.parse(sessionStorage.getItem("strategyLab.ownJob") ?? "{}").id).toBe("job-1");
      expect(runner.runPhase()).toBe(phase);
      expect(runner.runStatusBanner()).toBe(banner);
      expect(navigate).not.toHaveBeenCalled();
    });

    it("does not report a superseded remembered result's read failure against the new run", async () => {
      sessionStorage.setItem("strategyLab.ownJob", JSON.stringify({ id: "job-9", type: "engine_backtest" }));
      let reject: (reason: unknown) => void = () => undefined;
      fetchResult.mockReturnValueOnce(new Promise((_, rej) => { reject = rej; }));
      resumed.set(true);
      TestBed.tick();
      await Promise.resolve();

      await runner.run();
      reject(new HttpErrorResponse({ status: 500, statusText: "Server Error" }));
      await Promise.resolve();
      await Promise.resolve();
      await Promise.resolve();

      expect(runner.runError()).toBeNull();
      expect(runner.runPhase()).not.toBe("failed");
      expect(JSON.parse(sessionStorage.getItem("strategyLab.ownJob") ?? "{}").id).toBe("job-1");
    });

    it("reads a stored failure the same way the live path does", async () => {
      sessionStorage.setItem("strategyLab.ownJob", JSON.stringify({ id: "job-9", type: "engine_backtest" }));
      fetchResult.mockResolvedValue({ success: false, error: "boom" });
      resumed.set(true);
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();

      expect(navigate).not.toHaveBeenCalled();
      expect(runner.runError()).toBe("boom");
      expect(sessionStorage.getItem("strategyLab.ownJob")).toBeNull();
    });

    it("lets the job effects report a remembered job the registry already holds as terminal", async () => {
      // The snapshot can list the job in a terminal state (state is patched
      // before the active-set removal); the registry knows the outcome, so
      // no result is probed until the effect handles completion itself.
      sessionStorage.setItem("strategyLab.ownJob", JSON.stringify({ id: "job-9", type: "engine_backtest" }));
      fetchResult.mockResolvedValue({ success: true, study_id: 555, total_trades: 1, net_profit: 1 });
      putJob(makeJobState({ id: "job-9", type: "engine_backtest", status: "completed" }));
      resumed.set(true);
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();

      expect(navigate).toHaveBeenCalledWith(["/strategy-lab"], expect.objectContaining({ queryParams: { run: 555 } }));
    });

    it("reports a remembered job the registry holds as failed, without probing its result", () => {
      sessionStorage.setItem("strategyLab.ownJob", JSON.stringify({ id: "job-9", type: "engine_backtest" }));
      putJob(makeJobState({ id: "job-9", type: "engine_backtest", status: "failed", errorMessage: "engine crashed" }));
      resumed.set(true);
      TestBed.tick(); // adoption reattaches by id …
      TestBed.tick(); // … and the job effect reports the terminal state

      expect(fetchResult).not.toHaveBeenCalled();
      expect(runner.runError()).toBe("engine crashed");
    });

    it("still prefers the remembered job while it is active, even after the snapshot settles", () => {
      sessionStorage.setItem("strategyLab.ownJob", JSON.stringify({ id: "job-9", type: "engine_backtest" }));
      resumed.set(true);
      putJob(makeJobState({ id: "job-9", type: "engine_backtest", status: "running" }));
      TestBed.tick();

      expect(runner.running()).toBe(true);
      expect(fetchResult).not.toHaveBeenCalled();
    });

    it("leaves a parity companion's LEAN job alone even though it shares the job type", () => {
      putJob(
        makeJobState({
          id: "companion-lean",
          type: "lean_engine_run",
          status: "running",
          parameters: { request: { run_id: "companion-spy-x1" } },
        }),
      );
      putJob(makeJobState({ id: "unscoped-lean", type: "lean_engine_run", status: "running" }));
      TestBed.tick();

      expect(runner.running()).toBe(false);
    });

    it("does not guess between several active jobs when it has none of its own", () => {
      putJob(makeJobState({ id: "tab-a", type: "engine_backtest", status: "running" }));
      putJob(makeJobState({ id: "tab-b", type: "engine_backtest", status: "running" }));
      TestBed.tick();

      expect(runner.running()).toBe(false);
    });

    it("ends the run when its own job terminates instead of adopting another active job", async () => {
      await runner.run();
      putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "running" }));
      putJob(makeJobState({ id: "other-tab", type: "engine_backtest", status: "running" }));
      TestBed.tick();

      fetchResult.mockResolvedValue({ success: false, error: "boom" });
      putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "failed", errorMessage: "boom" }));
      TestBed.tick();
      await Promise.resolve();
      expect(runner.running()).toBe(false);

      // The other tab's job is still active, but this run is over: no
      // adoption, and its completion is not this tab's result.
      fetchResult.mockResolvedValue({ success: true, study_id: 777, total_trades: 1, net_profit: 1 });
      putJob(makeJobState({ id: "other-tab", type: "engine_backtest", status: "completed" }));
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();
      expect(runner.running()).toBe(false);
      expect(navigate).not.toHaveBeenCalled();
    });

    it("does not fall back to another tab's job while its own marker is stale", async () => {
      await runner.run();
      // This tab's job-1 never shows up as active (it ended while the tab was
      // away); another tab's single job is running.
      const reloaded = TestBed.runInInjectionContext(() => new StrategyLabRunner());
      putJob(makeJobState({ id: "other-tab", type: "engine_backtest", status: "running" }));
      TestBed.tick();

      expect(reloaded.running()).toBe(false);
      expect(reloaded.engineBusy()).toBe(true);
    });

    it("forgets its marker once its own job has ended", async () => {
      await runner.run();
      putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "running" }));
      TestBed.tick();
      fetchResult.mockResolvedValue({ success: false, error: "boom" });
      putJob(makeJobState({ id: "job-1", type: "engine_backtest", status: "failed", errorMessage: "boom" }));
      TestBed.tick();
      await Promise.resolve();

      // A later reload with one other active job is unambiguous again.
      const reloaded = TestBed.runInInjectionContext(() => new StrategyLabRunner());
      putJob(makeJobState({ id: "other-tab", type: "engine_backtest", status: "running" }));
      TestBed.tick();
      expect(reloaded.running()).toBe(true);
    });

    it("reports the container busy while several jobs are active even though none is adoptable", () => {
      putJob(makeJobState({ id: "tab-a", type: "engine_backtest", status: "running" }));
      putJob(makeJobState({ id: "tab-b", type: "engine_backtest", status: "running" }));
      TestBed.tick();

      expect(runner.running()).toBe(false);
      expect(runner.engineBusy()).toBe(true);
    });

    it("remembers a job adopted by fallback so a later reload prefers it over newer experiments", () => {
      putJob(makeJobState({ id: "solo", type: "engine_backtest", status: "running" }));
      TestBed.tick();
      expect(runner.running()).toBe(true);

      const reloaded = TestBed.runInInjectionContext(() => new StrategyLabRunner());
      putJob(makeJobState({ id: "later", type: "engine_backtest", status: "running" }));
      TestBed.tick();
      expect(reloaded.running()).toBe(true);
    });

    it("navigates to the produced study once an adopted engine_backtest job completes", async () => {
      fetchResult.mockResolvedValue({ success: true, study_id: 224, total_trades: 2, net_profit: 150 });
      putJob(makeJobState({ id: "resumed-3", type: "engine_backtest", status: "running" }));
      TestBed.tick();
      expect(runner.running()).toBe(true);

      putJob(makeJobState({ id: "resumed-3", type: "engine_backtest", status: "completed" }));
      TestBed.tick();
      await Promise.resolve();
      await Promise.resolve();

      expect(navigate).toHaveBeenCalledWith(
        ["/strategy-lab"],
        expect.objectContaining({ queryParams: { run: 224 }, queryParamsHandling: "merge" }),
      );
      expect(runner.running()).toBe(false);
    });
  });
});
