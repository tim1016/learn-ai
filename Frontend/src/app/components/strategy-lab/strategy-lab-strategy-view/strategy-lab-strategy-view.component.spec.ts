/** #2639 D13: a backtest's strategy view in Strategy Lab, the same panel the
 * bot page shows a bot in, read from a replay of the run's own window and
 * warmup, with the run's price-and-trades chart one tab away. */
import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting, type TestRequest } from "@angular/common/http/testing";
import { Component, input } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { render, screen, within } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { LeanSidecarService } from "../../../services/lean-sidecar.service";
import type { BacktestRunDetail } from "../../../services/backtest-runs.types";
import { fakeStrategyChartFactory } from "../../../testing/strategy-chart-fake";
import { fakeStrategyViewDataPlane } from "../../../testing/strategy-view-data-plane-fakes";
import { fakeStrategyView } from "../../../testing/strategy-view-fixtures";
import { BotChartPanelComponent } from "../../broker/v2-panel/strategy-view/bot-chart-panel.component";
import { STRATEGY_CHART_FACTORY } from "../../broker/v2-panel/strategy-view/strategy-chart.component";
import { makeRun } from "../testing/run-fixtures";
import { StrategyLabStrategyViewComponent } from "./strategy-lab-strategy-view.component";

vi.mock("lightweight-charts", () => ({
  createSeriesMarkers: vi.fn().mockReturnValue({ setMarkers: vi.fn() }),
  CandlestickSeries: "CandlestickSeries",
  HistogramSeries: "HistogramSeries",
  LineSeries: "LineSeries",
  TickMarkType: { Year: 0, Month: 1, DayOfMonth: 2, Time: 3, TimeWithSeconds: 4 },
}));

@Component({ selector: "app-strategy-lab-chart", template: `<p>Run price chart</p>` })
class RunChartStub {
  readonly run = input.required<BacktestRunDetail>();
  readonly markers = input<unknown[]>([]);
  readonly equityPoints = input<unknown[]>([]);
}

/** Each trading date's next session open, as the calendar answers it (arbitrary distinct instants). */
const SESSION_OPENS: Record<string, number> = {
  "2026-01-04": 1_767_623_400_000, // asked for the day before the window starts (2026-01-05)
  "2026-01-06": 1_767_796_200_000, // asked for the window's last day
  "2025-12-30": 1_767_105_000_000, // asked for the day before the warmup starts (2025-12-31)
};

const STRATEGY_VIEW_URL = "/api/engine/strategy-view";

async function renderView(run: BacktestRunDetail) {
  const charts = fakeStrategyChartFactory(vi);
  const nextTradingDayOpen = vi.fn(async (isoDate: string) => ({
    next_trading_date: isoDate,
    session_open_ms_utc: SESSION_OPENS[isoDate] ?? 0,
  }));
  const rendered = await render(StrategyLabStrategyViewComponent, {
    inputs: { run },
    componentImports: [BotChartPanelComponent, RunChartStub],
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: LeanSidecarService, useValue: { nextTradingDayOpen } },
      { provide: STRATEGY_CHART_FACTORY, useValue: charts.create },
      ...fakeStrategyViewDataPlane(vi).providers,
    ],
  });
  return { ...rendered, http: TestBed.inject(HttpTestingController), nextTradingDayOpen };
}

/** Lets the calendar reads settle, then hands back the strategy-view request they led to. */
async function strategyViewRequest(http: HttpTestingController): Promise<TestRequest> {
  // `match` takes what it finds off the pending list, so it is kept here.
  const found: TestRequest[] = [];
  await vi.waitFor(() => {
    found.push(...http.match((request) => request.url.endsWith(STRATEGY_VIEW_URL)));
    expect(found).toHaveLength(1);
  }, { timeout: 1000 });
  return found[0];
}

describe("StrategyLabStrategyViewComponent (#2639)", () => {
  it("draws the run's strategy view from a replay of its own settings, window and warmup", async () => {
    const user = userEvent.setup();
    const { http, fixture } = await renderView(makeRun({ warmupFromDate: 1_767_157_200_000 })); // 2025-12-31 ET midnight

    const request = await strategyViewRequest(http);
    expect(request.request.method).toBe("POST");
    expect(request.request.body).toEqual({
      strategy_name: "spy_ema_crossover",
      parameters: { short: 5, long: 10, symbol: "SPY" },
      symbol: "SPY",
      adjusted: true,
      session: "regular",
      // The window opens at the first evaluated session and closes at the session after the last.
      from_ms_utc: SESSION_OPENS["2026-01-04"],
      to_ms_utc: SESSION_OPENS["2026-01-06"],
      warmup_from_ms_utc: SESSION_OPENS["2025-12-30"],
    });
    request.flush(fakeStrategyView());
    await fixture.whenStable();

    expect(screen.getByRole("tab", { name: "Strategy · 15m", selected: true })).toBeTruthy();
    expect(screen.getByRole("group", { name: /Foo Cross decision candles for SPY/ })).toBeTruthy();
    expect(screen.queryByText("Run price chart")).toBeNull();

    await user.click(screen.getByRole("tab", { name: "Price & trades" }));
    expect(screen.getByText("Run price chart")).toBeTruthy();
  });

  it("asks for no warmup when the run read no history before its window", async () => {
    const { http } = await renderView(makeRun({ warmupFromDate: null }));

    const request = await strategyViewRequest(http);
    expect(request.request.body.warmup_from_ms_utc).toBeNull();
    request.flush(fakeStrategyView());
  });

  it("says why a run that recorded no data policy has no strategy view, and asks for nothing", async () => {
    const { http } = await renderView(makeRun({ dataPolicy: null }));

    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("This run’s strategy view is unavailable.")).toBeTruthy();
    expect(within(alert).getByText("The run recorded no data policy, so its bars cannot be replayed.")).toBeTruthy();
    http.expectNone((request) => request.url.endsWith(STRATEGY_VIEW_URL));
  });

  it("shows the engine's reason when the read is refused, and Retry reads again", async () => {
    const user = userEvent.setup();
    const { http } = await renderView(makeRun());

    (await strategyViewRequest(http)).flush(
      { detail: "unknown strategy: spy_ema_crossover" },
      { status: 400, statusText: "Bad Request" },
    );
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("This run’s strategy view could not be read.")).toBeTruthy();
    expect(within(alert).getByText("unknown strategy: spy_ema_crossover")).toBeTruthy();

    await user.click(within(alert).getByRole("button", { name: "Retry strategy view" }));
    (await strategyViewRequest(http)).flush(fakeStrategyView());
    expect(await screen.findByRole("group", { name: /decision candles for SPY/ })).toBeTruthy();
  });
});
