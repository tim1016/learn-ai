/** #2639 D13: a backtest's strategy view in Strategy Lab, the same panel the
 * bot page shows a bot in, read as a replay of the saved run, with the run's
 * price-and-trades chart one tab away. */
import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting, type TestRequest } from "@angular/common/http/testing";
import { Component, input } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { render, screen, within } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

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

async function renderView(run: BacktestRunDetail) {
  const charts = fakeStrategyChartFactory(vi);
  const rendered = await render(StrategyLabStrategyViewComponent, {
    inputs: { run },
    componentImports: [BotChartPanelComponent, RunChartStub],
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      { provide: STRATEGY_CHART_FACTORY, useValue: charts.create },
      ...fakeStrategyViewDataPlane(vi).providers,
    ],
  });
  return { ...rendered, http: TestBed.inject(HttpTestingController) };
}

function strategyViewRequest(http: HttpTestingController, runId: number): TestRequest {
  return http.expectOne((request) => request.url.endsWith(`/api/research/backtest-runs/${runId}/strategy-view`));
}

describe("StrategyLabStrategyViewComponent (#2639)", () => {
  it("draws the saved run's own strategy view, with its price chart one tab away", async () => {
    const user = userEvent.setup();
    const run = makeRun();
    const { http, fixture } = await renderView(run);

    const request = strategyViewRequest(http, run.id);
    expect(request.request.method).toBe("GET");
    request.flush(fakeStrategyView());
    await fixture.whenStable();

    expect(screen.getByRole("tab", { name: "Strategy · 15m", selected: true })).toBeTruthy();
    expect(screen.getByRole("group", { name: /Foo Cross decision candles for SPY/ })).toBeTruthy();
    expect(screen.queryByText("Run price chart")).toBeNull();

    await user.click(screen.getByRole("tab", { name: "Price & trades" }));
    expect(screen.getByText("Run price chart")).toBeTruthy();
  });

  it("says why in the data plane's words when the run cannot be replayed exactly, and Retry reads again", async () => {
    const user = userEvent.setup();
    const run = makeRun();
    const { http } = await renderView(run);

    strategyViewRequest(http, run.id).flush(
      {
        detail: {
          code: "STRATEGY_VIEW_NOT_REPLAYABLE",
          message: "The strategy has changed since this run: it ran 1.0.0, and this build has 1.1.0.",
        },
      },
      { status: 409, statusText: "Conflict" },
    );
    const alert = await screen.findByRole("alert");
    expect(within(alert).getByText("This run’s strategy view could not be shown.")).toBeTruthy();
    expect(within(alert).getByText("The strategy has changed since this run: it ran 1.0.0, and this build has 1.1.0.")).toBeTruthy();

    await user.click(within(alert).getByRole("button", { name: "Retry strategy view" }));
    strategyViewRequest(http, run.id).flush(fakeStrategyView());
    expect(await screen.findByRole("group", { name: /decision candles for SPY/ })).toBeTruthy();
  });
});
