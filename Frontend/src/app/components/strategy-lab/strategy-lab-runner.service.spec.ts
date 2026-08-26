import { provideHttpClient } from "@angular/common/http";
import { signal } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { ActivatedRoute, convertToParamMap } from "@angular/router";
import { of } from "rxjs";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { JobsService } from "../../services/jobs.service";
import { LeanSidecarService } from "../../services/lean-sidecar.service";
import { StrategyLabConfigStore } from "./strategy-lab-config.store";
import { StrategyLabRunner } from "./strategy-lab-runner.service";
import type { StrategyInfo } from "./strategy-lab.models";

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
  let nextTradingDayOpen: ReturnType<typeof vi.fn>;
  let diagnose: ReturnType<typeof vi.fn>;

  beforeEach(() => {
    startJob = vi.fn(async () => "job-1");
    diagnose = vi.fn(async () => ({
      overall_status: "pass",
      checks: [{ name: "launcher_healthz", status: "pass", detail: "ready" }],
    }));
    nextTradingDayOpen = vi.fn(async (date: string) => ({
      session_open_ms_utc: date === "2026-01-04" ? 1_767_624_600_000 : 1_768_000_000_000,
    }));
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        StrategyLabConfigStore,
        StrategyLabRunner,
        { provide: ActivatedRoute, useValue: { queryParamMap: of(convertToParamMap({})) } },
        {
          provide: JobsService,
          useValue: {
            jobs: signal([]),
            job: vi.fn(() => null),
            startJob,
            fetchResult: vi.fn(),
          },
        },
        {
          provide: LeanSidecarService,
          useValue: {
            diagnose,
            nextTradingDayOpen,
          },
        },
      ],
    });
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
  });

  it("keeps data-policy cadence and adjustment choices in the configuration store", () => {
    config.engine.set("python");
    expect(config.dataPolicy()).toEqual(expect.objectContaining({
      adjusted: true,
      input_bars: { timespan: "minute", multiplier: 1 },
      strategy_bars: { timespan: "minute", multiplier: 15 },
    }));
  });

  it("removes GraphQL metadata before a restored data policy reaches the engine", async () => {
    const restoredPolicy = {
      ...config.dataPolicy(),
      __typename: "DataPolicyType",
      input_bars: {
        ...config.dataPolicy().input_bars,
        __typename: "BarsSpecType",
      },
      strategy_bars: {
        ...config.dataPolicy().strategy_bars,
        __typename: "BarsSpecType",
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
    expect(JSON.stringify(submittedPayload)).not.toContain("__typename");
  });

  it("selects a runnable strategy after an ordinary engine change", () => {
    const pythonOnly = { ...STRATEGY, name: "python_only", lean_twin: null };
    config.strategies.set([pythonOnly, STRATEGY]);
    config.selectStrategy(pythonOnly.name);

    config.changeEngine("lean");
    TestBed.tick();

    expect(config.selectedStrategyName()).toBe(STRATEGY.name);
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

  it("forwards configurable gap and RSI gates to the aligned LEAN template", async () => {
    config.strategies.set([PARAMETERIZED_STRATEGY]);
    config.selectStrategy(PARAMETERIZED_STRATEGY.name);
    config.updateParameter("gap_bps", "4", "number");
    config.updateParameter("rsi_min", "52", "number");
    config.updateParameter("rsi_max", "68", "number");
    config.engine.set("lean");

    await runner.run();

    expect(startJob).toHaveBeenCalledWith(
      "lean_engine_run",
      expect.objectContaining({
        request: expect.objectContaining({
          template: "ema_crossover_2_bps",
          strategy_parameters: { gap_bps: 4, rsi_min: 52, rsi_max: 68 },
        }),
      }),
    );
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
});
