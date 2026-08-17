import { HttpClient } from "@angular/common/http";
import { signal } from "@angular/core";
import { fireEvent, render, screen } from "@testing-library/angular";
import { of } from "rxjs";
import { describe, expect, it, vi } from "vitest";

import { RecencyLaunchConfigComponent } from "./recency-launch-config.component";
import { JobsService } from "../../../../services/jobs.service";
import type { StrategyInfo } from "../../../strategy-lab/strategy-lab.models";

function makeStrategy(overrides: Partial<StrategyInfo> = {}): StrategyInfo {
  return {
    name: "ema_crossover_2_bps",
    display_name: "EMA Crossover (2 bps)",
    description: "",
    params_schema: {
      properties: {
        symbol: { type: "string", default: "SPY" },
        gap_bps: { type: "number", default: 2, title: "Crossover gap (bps)" },
        rsi_min: { type: "number", default: 50, title: "RSI lower gate" },
      },
    },
    supported_resolutions: ["minute"],
    strategy_bars: { timespan: "minute", multiplier: 15 },
    recency_supported: true,
    ...overrides,
  };
}

async function renderConfig(
  strategies: StrategyInfo[],
  startJob = vi.fn(async () => "job-1"),
  job: (id: string) => { status: string } | undefined = () => undefined,
) {
  const view = await render(RecencyLaunchConfigComponent, {
    providers: [
      { provide: HttpClient, useValue: { get: () => of(strategies) } },
      { provide: JobsService, useValue: { startJob, job } },
    ],
  });
  return { view, startJob };
}

describe("RecencyLaunchConfigComponent", () => {
  it("lists only recency-supported strategies in the picker", async () => {
    await renderConfig([
      makeStrategy({ name: "ema_crossover_2_bps", display_name: "EMA Crossover (2 bps)", recency_supported: true }),
      makeStrategy({ name: "spy_ema_crossover_options", display_name: "EMA Options Spread", recency_supported: false }),
    ]);

    expect(screen.getByText("EMA Crossover (2 bps)")).not.toBeNull();
    expect(screen.queryByText("EMA Options Spread")).toBeNull();
  });

  it("shows a range input per numeric param (excluding symbol) once a strategy is selected", async () => {
    const { view } = await renderConfig([makeStrategy()]);

    fireEvent.click(screen.getByRole("checkbox", { name: /ema crossover \(2 bps\)/i }));
    await view.fixture.whenStable();

    expect(screen.getByText("Crossover gap (bps)")).not.toBeNull();
    expect(screen.getByText("RSI lower gate")).not.toBeNull();
    expect(screen.queryByLabelText(/^symbol$/i)).toBeNull();
  });

  it("computes the pre-launch run count from symbols and selected strategy ranges", async () => {
    const { view } = await renderConfig([makeStrategy()]);

    fireEvent.input(screen.getByLabelText(/symbols/i), { target: { value: "SPY, AAPL" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /ema crossover \(2 bps\)/i }));
    await view.fixture.whenStable();

    // default seeding: each numeric param starts as a single-value list -> 1 combo per symbol
    expect(screen.getByText(/2 runs?/i)).not.toBeNull();
  });

  it("launches a recency_chart job with the selected symbols and strategy ranges", async () => {
    const { view, startJob } = await renderConfig([makeStrategy()]);

    fireEvent.input(screen.getByLabelText(/symbols/i), { target: { value: "SPY" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /ema crossover \(2 bps\)/i }));
    fireEvent.click(screen.getByRole("button", { name: /launch/i }));
    await view.fixture.whenStable();

    expect(startJob).toHaveBeenCalledWith(
      "recency_chart",
      expect.objectContaining({
        symbols: ["SPY"],
        strategies: expect.arrayContaining([
          expect.objectContaining({ strategyKey: "ema_crossover_2_bps" }),
        ]),
      }),
    );
  });

  it("emits launchCompleted once the launched job's status turns completed", async () => {
    const jobState = signal<{ status: string } | undefined>(undefined);
    const startJob = vi.fn(async () => "job-1");
    const { view } = await renderConfig([makeStrategy()], startJob, (id: string) =>
      id === "job-1" ? jobState() : undefined,
    );
    const onCompleted = vi.fn();
    view.fixture.componentInstance.launchCompleted.subscribe(onCompleted);

    fireEvent.input(screen.getByLabelText(/symbols/i), { target: { value: "SPY" } });
    fireEvent.click(screen.getByRole("checkbox", { name: /ema crossover \(2 bps\)/i }));
    fireEvent.click(screen.getByRole("button", { name: /launch/i }));
    await view.fixture.whenStable();

    expect(onCompleted).not.toHaveBeenCalled();

    jobState.set({ status: "completed" });
    await view.fixture.whenStable();

    expect(onCompleted).toHaveBeenCalledTimes(1);
  });

  it("caps the custom duration input at 24 months", async () => {
    const { view } = await renderConfig([makeStrategy()]);

    fireEvent.click(screen.getByRole("radio", { name: /custom/i }));
    fireEvent.input(screen.getByLabelText(/custom months/i), { target: { value: "99" } });
    await view.fixture.whenStable();

    expect((screen.getByLabelText(/custom months/i) as HTMLInputElement).value).toBe("24");
  });
});
