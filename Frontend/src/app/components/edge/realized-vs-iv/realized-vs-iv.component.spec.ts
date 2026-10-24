import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { TestBed } from "@angular/core/testing";
import { provideRouter } from "@angular/router";
import { render, screen, within } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { JobsService } from "../../../services/jobs.service";
import { DataLakeService, type DataLakeRead, type InstrumentDataReport } from "../../../shared/data-lake";
import { heldReport } from "../../../shared/data-lake/testing/instrument-data-report";
import { TradeBackfillRunner, type TradeBackfillEnd } from "../../../shared/data-lake/trade-backfill-runner";
import { fakePickerWorld } from "../../../shared/symbol-picker/testing/fake-picker-world";
import type {
  IvConfidenceSummary,
  LiveIv30State,
  RealizedVsIvData,
} from "../services/edge-data.model";
import {
  answerCompute, answerWindow, failCompute, FakeJobs, lakeDataSource, refuseCompute, takeWindowRequest, tick, YZ_RV_BY_WINDOW,
  type ComputeWorld,
} from "../testing/edge-compute-world";
import { mapLakeStudyDataSource } from "../../../shared/lake-study/lake-study";
import { environment } from "../../../../environments/environment";
import { RECORD_ID, RVIV_INPUTS, storedRecord } from "../../../research/stored-study/testing/stored-study-fixtures";
import { RealizedVsIvComponent } from "./realized-vs-iv.component";

async function renderPage(inputs: Record<string, unknown> = {}) {
  const jobs = new FakeJobs();
  // The lake's count of the sessions the window needs (the page reads it once per window), and the shared capture. Left
  // alone, the lake holds every session, so only the specs about the count see a warning.
  const instrumentData = vi.fn<(query: unknown) => Promise<DataLakeRead<InstrumentDataReport>>>()
    .mockResolvedValue({ kind: "ok", value: heldReport({ sessions: 273 }) });
  const captureRun = vi.fn<(select: unknown, accepted: unknown) => Promise<TradeBackfillEnd>>()
    .mockResolvedValue({ kind: "completed", failure: null });
  const view = await render(RealizedVsIvComponent, {
    inputs,
    providers: [
      ...fakePickerWorld().providers,
      { provide: JobsService, useValue: jobs },
      { provide: DataLakeService, useValue: { instrumentData } },
      { provide: TradeBackfillRunner, useValue: { run: captureRun } },
      provideHttpClient(),
      provideHttpClientTesting(),
      provideRouter([]),
    ],
  });
  const world: ComputeWorld = { http: TestBed.inject(HttpTestingController), jobs };
  return { ...view, world, user: userEvent.setup(), instrumentData, captureRun };
}

/** A real-shaped result, for states that are easier to set than to drive
 *  through a job and the live-IV requests. */
function realData(overrides: Partial<RealizedVsIvData> = {}): RealizedVsIvData {
  const N = 12;
  const flat = (v: number) => Array.from({ length: N }, () => v);
  return {
    provenance: "real",
    symbol: "SPY",
    N,
    dates: Array.from({ length: N }, (_, i) => new Date(Date.UTC(2026, 0, 5 + i, 14))),
    candles: Array.from({ length: N }, (_, i) => ({ o: 500 + i, h: 503 + i, l: 498 + i, c: 501 + i, v: 1e6 })),
    iv30: flat(0.21),
    iv30Trd252: flat(0.2),
    rvYZ: flat(0.14),
    rvYzByWindow: [5, 10, 30].map((window) => ({ window, values: flat(0.14) })),
    rvHf21d: flat(0.15),
    vrpForward: flat(0.05),
    vrpZ: flat(0),
    vrpHistogram: [],
    signals: [],
    coverage: { forwardBlindTail: 2, vrpLookbackBars: N - 1, ivFirstTsMs: Date.UTC(2026, 0, 6, 14) },
    ivConfidence: null,
    ivAlignment: { rule: "snapshot_in_bar_latest", snapshotsRead: N, barsWithIv: N, superseded: 0, outsideBars: 0 },
    ivInput: { source: "recorder", snapshots: N, digest: "c".repeat(64) },
    run: { barSize: "1D", tenorDays: 30, hfSession: null },
    dataSource: mapLakeStudyDataSource(lakeDataSource()),
    liveIv30: { kind: "unavailable", reason: "Neither live IV30 endpoint answered." },
    ...overrides,
  };
}

const chartCount = () => document.querySelectorAll("canvas").length;

/** Lets the page's effects and promises run. `fixture.whenStable()` is no use while a request is open: an unanswered
 *  request is a pending task, and the page always has one open until the test answers it. */
async function settle(fixture: { detectChanges(): void }): Promise<void> {
  await tick();
  fixture.detectChanges();
  await tick();
  fixture.detectChanges();
}

describe("RealizedVsIvComponent — opens idle", () => {
  it("shows no study, no chart and no sample label before anything is computed", async () => {
    await renderPage();

    expect(screen.getByText("No result yet.")).toBeTruthy();
    expect(chartCount(), "no chart is drawn from placeholder values").toBe(0);
    expect(screen.queryByTestId("rviv-readout")).toBeNull();
    expect(screen.queryByTestId("rviv-source-tag")).toBeNull();
    expect(screen.queryByTestId("sample-data-banner")).toBeNull();
    expect(screen.queryByTestId("rviv-error")).toBeNull();
    expect(screen.queryByTestId("rviv-refused")).toBeNull();
  });
});

describe("RealizedVsIvComponent — the controls show what the run uses", () => {
  const select = (testId: string) => screen.getByTestId(testId) as HTMLSelectElement;

  it("opens with the bar size and tenor the first Compute would use, not the first option of each list", async () => {
    await renderPage();

    expect(select("rviv-bar-size").value).toBe("1D");
    expect(select("rviv-tenor").value).toBe("30D");
  });

  it("offers only the bar sizes the study is defined for", async () => {
    await renderPage();

    const options = (testId: string) => within(select(testId)).getAllByRole("option").map((o) => o.textContent?.trim());
    expect(options("rviv-bar-size")).toEqual(["15m", "1D"]);
    expect(options("rviv-tenor")).toEqual(["7D", "14D", "30D", "60D"]);
  });

  it("runs the bar size and tenor on screen, says so beside the result, and keeps showing them", async () => {
    const { world, user, fixture } = await renderPage();

    await user.selectOptions(select("rviv-bar-size"), "15m");
    await user.selectOptions(select("rviv-tenor"), "60D");
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world, { run: { bar_size: "15m", tenor_days: 60, hf_session: "ETH" } });
    fixture.detectChanges();

    expect(world.jobs.started[0].payload).toMatchObject({ bar_size: "15m", tenor_days: 60, session: "extended", hf_session: "ETH" });
    expect(select("rviv-bar-size").value).toBe("15m");
    expect(select("rviv-tenor").value).toBe("60D");
    expect(screen.getByTestId("rviv-run-settings").textContent?.replace(/\s+/g, " ").trim()).toBe("15m bars · 60D tenor · HF ETH");
  });

  it("says which settings produced the result on screen even after a control is changed without computing again", async () => {
    const { world, user, fixture } = await renderPage();
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world);
    fixture.detectChanges();

    await user.selectOptions(select("rviv-bar-size"), "15m");
    fixture.detectChanges();

    expect(screen.getByTestId("rviv-run-settings").textContent?.replace(/\s+/g, " ").trim()).toBe("1D bars · 30D tenor");
  });

});

describe("RealizedVsIvComponent — the session the bars are folded from", () => {
  const select = (testId: string) => screen.getByTestId(testId) as HTMLSelectElement;
  const sessionButton = (name: "RTH" | "Ext") => within(screen.getByTestId("rviv-session")).getByRole("button", { name });
  const pressed = (name: "RTH" | "Ext") => sessionButton(name).getAttribute("aria-pressed") === "true";

  it("offers the session for 15-minute bars only: daily bars read regular hours and have no choice", async () => {
    const { user } = await renderPage();
    expect(screen.queryByTestId("rviv-session"), "daily is the opening size").toBeNull();

    await user.selectOptions(select("rviv-bar-size"), "15m");
    expect(screen.getByTestId("rviv-session")).toBeTruthy();

    await user.selectOptions(select("rviv-bar-size"), "1D");
    expect(screen.queryByTestId("rviv-session")).toBeNull();
  });

  it("opens a 15-minute run on extended hours, the session the study has always read", async () => {
    const { user } = await renderPage();

    await user.selectOptions(select("rviv-bar-size"), "15m");

    expect(pressed("Ext")).toBe(true);
    expect(pressed("RTH")).toBe(false);
  });

  it("sends extended hours with the HF estimator's ETH session when nothing is changed", async () => {
    const { world, user } = await renderPage();

    await user.selectOptions(select("rviv-bar-size"), "15m");
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await tick();

    expect(world.jobs.started[0].payload).toMatchObject({ bar_size: "15m", session: "extended", hf_session: "ETH" });
  });

  it("sends regular hours with the HF estimator's RTH session when the user chooses RTH", async () => {
    const { world, user } = await renderPage();

    await user.selectOptions(select("rviv-bar-size"), "15m");
    await user.click(sessionButton("RTH"));
    expect(pressed("RTH")).toBe(true);
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await tick();

    expect(world.jobs.started[0].payload).toMatchObject({ bar_size: "15m", session: "rth", hf_session: "RTH" });
  });

  it("sends regular hours for a daily run whatever 15-minute session was chosen before, and no HF session", async () => {
    const { world, user } = await renderPage();
    await user.selectOptions(select("rviv-bar-size"), "15m");
    await user.click(sessionButton("Ext"));
    await user.selectOptions(select("rviv-bar-size"), "1D");

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await tick();

    expect(world.jobs.started[0].payload).toMatchObject({ bar_size: "1d", session: "rth" });
    expect(world.jobs.started[0].payload).not.toHaveProperty("hf_session");
  });

  it("carries the session into the ready summary", async () => {
    const { user } = await renderPage();
    const summary = () => document.querySelector(".study-run__summary")?.textContent?.replace(/\s+/g, " ").trim() ?? "";
    expect(summary()).toContain("SPY, 1D bars, regular hours, 30D tenor");

    await user.selectOptions(select("rviv-bar-size"), "15m");
    expect(summary()).toContain("SPY, 15m bars, extended hours, 30D tenor");

    await user.click(sessionButton("RTH"));
    expect(summary()).toContain("SPY, 15m bars, regular hours, 30D tenor");
  });

  it("marks a shown result as previous when the session changes, and names the session it answered", async () => {
    const { world, user, fixture } = await renderPage();
    await user.selectOptions(select("rviv-bar-size"), "15m");
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world, { run: { bar_size: "15m", tenor_days: 30, hf_session: "ETH" } });
    fixture.detectChanges();
    expect(screen.queryByText("Previous result"), "nothing has changed since the run").toBeNull();

    await user.click(sessionButton("RTH"));

    const banner = screen.getByText("Previous result").closest("div");
    if (banner === null) throw new Error("no previous-result banner");
    expect(within(banner).getByText("extended hours"), "the session it was computed for").toBeTruthy();
  });
});

describe("RealizedVsIvComponent — the page says what it will run", () => {
  it("names the window the study fixes instead of offering dates", async () => {
    await renderPage();

    expect(screen.getByTestId("study-constraints").textContent).toContain("273 daily bars");
  });
});

describe("RealizedVsIvComponent — one press, one run", () => {
  it("starts nothing when an input changes, only when Run study is pressed", async () => {
    const { world, user } = await renderPage();

    await user.selectOptions(screen.getByLabelText("Bar size"), "15m");
    await user.selectOptions(screen.getByLabelText("Tenor"), "60D");
    await user.click(screen.getByRole("button", { name: "RTH" }));

    expect(world.jobs.started).toHaveLength(0);
    world.http.expectNone((request) => request.url.startsWith("/api/edge/iv30"));
  });

  it("starts one job when the second click lands before the page has re-rendered", async () => {
    const { world } = await renderPage();
    const run = screen.getByRole("button", { name: "Run study" });

    // Two clicks in one task: the button is not disabled yet when the second arrives.
    run.click();
    run.click();
    await tick();

    expect(world.jobs.started).toHaveLength(1);
  });

  it("marks the result as previous once an input changes, and keeps what it answered", async () => {
    const { world, user, fixture } = await renderPage();
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world);
    fixture.detectChanges();

    await user.selectOptions(screen.getByLabelText("Tenor"), "60D");

    const banner = screen.getByText("Previous result").closest("div");
    if (banner === null) throw new Error("no previous-result banner");
    expect(within(banner).getByText("30D"), "the tenor it was computed for").toBeTruthy();
  });
});

describe("RealizedVsIvComponent — descriptive result", () => {
  it("shows what was measured and no pass/fail verdict", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world);
    fixture.detectChanges();

    expect(screen.getByTestId("study-descriptive-note")).toBeTruthy();
    expect(screen.getByTestId("rviv-readout")).toBeTruthy();
    const result = screen.getByRole("region", { name: "Result" });
    expect(result.textContent ?? "").not.toMatch(/verdict|\bpass(ed)?\b|\bfail(ed)?\b|graduat|stage \d/i);
  });
});

describe("RealizedVsIvComponent — real response", () => {
  it("draws the returned study and reads edge score as unavailable, not zero", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world);
    fixture.detectChanges();

    expect(screen.getByTestId("rviv-source-tag").textContent).toContain("SPY");
    expect(screen.queryByTestId("rviv-idle")).toBeNull();
    expect(chartCount(), "price chart and VRP histogram").toBe(2);
    const readout = screen.getByTestId("rviv-readout").textContent ?? "";
    expect.soft(readout, "values come from the response").toContain("21.0%");
    expect.soft(readout, "values come from the response").toContain("14.0%");
    expect(screen.getByTestId("rviv-edge-score-unavailable").textContent).toBe("unavailable");
    expect(readout, "no zero or default masquerading as a measurement").not.toMatch(/edge_score\s*[+-]?0\.00/);
    expect(screen.getByTestId("rviv-unavailable-note")).toBeTruthy();
    expect(screen.queryByTestId("sample-data-banner"), "nothing on this page is synthetic").toBeNull();
  });

  it("reports the 30-bar Yang-Zhang series as rv_yz_30_daily, not the first series the server sent", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world);
    fixture.detectChanges();

    const key = screen.getByText("rv_yz_30_daily");
    expect(key.nextElementSibling?.textContent?.trim()).toBe(`${(YZ_RV_BY_WINDOW[30] * 100).toFixed(1)}%`);
    expect(screen.getByTestId("rviv-readout").textContent, "the 5-bar series is not the readout").not.toContain(
      `${(YZ_RV_BY_WINDOW[5] * 100).toFixed(1)}%`,
    );
  });

  it("names the Yang-Zhang readout for the bars it was measured on: 15-minute bars are not daily bars", async () => {
    const { world, user, fixture } = await renderPage();

    await user.selectOptions(screen.getByTestId("rviv-bar-size"), "15m");
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world, { run: { bar_size: "15m", tenor_days: 30, hf_session: "ETH" } });
    fixture.detectChanges();

    expect(screen.getByText("rv_yz_30_15m")).toBeTruthy();
    expect(screen.queryByText("rv_yz_30_daily")).toBeNull();
  });

  it("offers no estimator chips, since the page computes Yang-Zhang alone", async () => {
    await renderPage();

    for (const name of ["CtC", "Parkinson", "GK"]) {
      expect(screen.queryByRole("button", { name }), name).toBeNull();
    }
  });

  it("keeps the forward-realized tail unavailable, saying how many bars", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world);
    fixture.detectChanges();

    expect(screen.getByText(/Forward RV unavailable for last 3 bars/)).toBeTruthy();
  });
});

describe("RealizedVsIvComponent — where the bars came from", () => {
  it("prints the data-source line with the lake, the window, the resolution and the added, reused and missing counts", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world);
    fixture.detectChanges();

    const summary = screen.getByText("Data source").closest("summary");
    expect(summary?.textContent).toContain("Data lake, Polygon");
    expect(summary?.textContent).toContain("daily bars, folded from 1-minute bars");
    expect(screen.getByText("10 added · 30 reused · 0 missing")).toBeTruthy();
  });

  it("names the IV input as not from the lake, with how many snapshots it read and their fingerprint", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world);
    fixture.detectChanges();

    const note = screen.getByTestId("rviv-iv-input").textContent?.replace(/\s+/g, " ") ?? "";
    expect(note).toContain("IV30 input, not from the lake");
    expect(note).toContain("IV recorder's file store, 35 snapshots in the window");
    expect(note).toContain("fingerprint cccccccccccc");
  });

  it("says there is no IV input at all when nothing was recorded or supplied, rather than naming a source", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world, { ivPresent: false });
    fixture.detectChanges();

    const note = screen.getByTestId("rviv-iv-input").textContent?.replace(/\s+/g, " ") ?? "";
    expect(note).toContain("none: nothing recorded for this window and none supplied");
    expect(note).not.toContain("fingerprint");
  });

  it("shows a refused run as no result, with the server's reason and the counts, and draws nothing", async () => {
    const { world, user, fixture } = await renderPage();
    const refusal = {
      success: false, status: "refused", symbol: "SPY",
      error: "The lake cannot admit this window.",
      refusal: {
        code: "data_unavailable_from_provider",
        reason: "The lake cannot admit this window: 1 of 252 sessions cannot be supplied by the provider: 2025-06-04 (provider_no_data).",
        sessions: [{ group: "window", trading_date_ms: Date.UTC(2025, 5, 4, 13, 30), outcome: "unavailable_from_provider", reason: "provider_no_data", failure_reason: "provider_no_data" }],
        sessions_total: 1,
        coverage: {
          window: { expected_sessions: 252, reused: 200, added: 51, missing: 0, failed: 1, building: 0, corrupt: 0, captured_before_final: 0, unavailable_from_provider: 1 },
          warmup: { expected_sessions: 0, reused: 0, added: 0, missing: 0, failed: 0, building: 0, corrupt: 0, captured_before_final: 0, unavailable_from_provider: 0 },
          artifacts: { fetched: 51, reused: 200, refreshed: 0 },
        },
      },
      data_source: lakeDataSource({ snapshot_digest: null, effective_window: null }),
    };

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await refuseCompute(world, refusal);
    fixture.detectChanges();

    const refused = screen.getByTestId("rviv-refused");
    expect(refused.textContent).toContain("No result");
    expect(refused.textContent).toContain("1 of 252 sessions cannot be supplied by the provider");
    expect(refused.textContent).toContain("252 sessions · 200 reused · 51 added · 0 missing · 1 failed");
    expect(chartCount()).toBe(0);
    expect(screen.queryByTestId("rviv-readout")).toBeNull();
    expect(screen.queryByTestId("rviv-idle"), "the page is not idle: it was refused").toBeNull();
    expect(screen.queryByTestId("rviv-error")).toBeNull();
  });

  it("refuses a bar size the study was not defined for with the server's reason, as a refusal and not an error", async () => {
    const { world, user, fixture } = await renderPage();
    const reason = "5m bars are not supported: the annualization and the HF estimator are defined for 15m and 1d bars only.";

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await refuseCompute(world, {
      success: false, status: "refused", symbol: "SPY", error: reason,
      refusal: { code: "resolution_unsupported", reason, sessions: [], sessions_total: 0, coverage: null }, data_source: null,
    });
    fixture.detectChanges();

    expect(screen.getByTestId("rviv-refused").textContent).toContain(reason);
    expect(screen.queryByText(/added · .* reused/), "nothing was read, so no counts are claimed").toBeNull();
  });
});

describe("RealizedVsIvComponent — no IV at these bars", () => {
  it("says there is no IV30 when the response carries none, rather than drawing an IV line, and why", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world, { ivPresent: false });
    fixture.detectChanges();

    expect(document.querySelector(".banner-iv-missing")).not.toBeNull();
    expect(screen.getByTestId("rviv-readout").textContent).toContain("—");
    expect(screen.getByTestId("iv-missing-reason").textContent).toContain("The IV recorder holds no snapshot for SPY in this window");
  });

  it("says when snapshots exist but none lands inside a bar, instead of claiming the recorder is empty", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world, {
      ivPresent: false,
      alignment: { snapshots_read: 3, bars_with_iv: 0, superseded: 0, outside_bars: 3 },
    });
    fixture.detectChanges();

    const reason = screen.getByTestId("iv-missing-reason").textContent ?? "";
    expect(reason).toContain("SPY");
    expect(reason).toMatch(/none falls inside a bar/);
    expect(reason).not.toContain("holds no snapshot");
  });

  it("counts how many snapshots reached a bar when only some did, and says nothing is carried forward", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world, { alignment: { snapshots_read: 40, bars_with_iv: 35, superseded: 2, outside_bars: 3 } });
    fixture.detectChanges();

    const line = screen.getByTestId("iv-alignment").textContent?.replace(/\s+/g, " ") ?? "";
    expect(line).toContain("35 of 40 bars hold an IV30");
    expect(line).toContain("40 snapshots read");
    expect(line).toContain("2 superseded by a later one in the same bar");
    expect(line).toContain("3 inside no bar");
    expect(line).toContain("nothing is carried forward");
  });

  it("never puts the current IV on a past date: with no recorded IV the live value appears only in its own row", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world, { ivPresent: false, live: "vix" });
    fixture.detectChanges();

    const readout = screen.getByTestId("rviv-readout");
    const ivRow = within(readout).getByText("iv30", { exact: true });
    expect(ivRow.nextElementSibling?.textContent?.trim(), "the series IV at the middle bar stays empty").toBe("—");
    expect(screen.getByTestId("live-iv30-readout").textContent).toContain("21.3%");
    expect(document.querySelector(".banner-iv-missing"), "and the page still says there is no IV30 at these bars").not.toBeNull();
  });
});

describe("RealizedVsIvComponent — no VRP z-score in the window", () => {
  const none = (n: number) => Array.from({ length: n }, () => Number.NaN);

  async function show(overrides: Partial<RealizedVsIvData>): Promise<void> {
    const { fixture } = await renderPage();
    fixture.componentInstance.data.set(realData(overrides));
    fixture.detectChanges();
  }

  const note = () => screen.queryByTestId("rviv-signal-unavailable")?.textContent?.replace(/\s+/g, " ") ?? null;

  it("says the window is too short when too few of its bars have a forward realized volatility", async () => {
    await show({ vrpZ: none(12), coverage: { forwardBlindTail: 3, vrpLookbackBars: 11, ivFirstTsMs: null } });

    expect(note()).toContain("VRP signals (the long-vol and short-vol markers, the Realtime toggle and vrp_z): none in this window.");
    expect(note()).toContain("measured over 11 bars in a row that each have a forward realized volatility");
    expect(note()).toContain("This window has 9 of 12 (the last 3 are not realized yet)");
    expect(note()).toContain("needs a longer window");
  });

  it("says IV30 must be on every bar of the z-score's window, and is not carried, when the window is long enough", async () => {
    await show({
      vrpZ: none(12),
      coverage: { forwardBlindTail: 1, vrpLookbackBars: 11, ivFirstTsMs: null },
      ivAlignment: { rule: "snapshot_in_bar_latest", snapshotsRead: 4, barsWithIv: 4, superseded: 0, outsideBars: 0 },
    });

    expect(note()).toContain("measured over 11 bars in a row that each have IV30, and IV30 is never carried from one bar to the next");
    expect(note()).toContain("IV30 is on 4 of this window's 12 bars, and no 11 in a row have it");
    expect(note()).not.toContain("forward realized volatility");
  });

  it("says nothing, and keeps the banner's claim that the signal is active, when the window has a z-score", async () => {
    await show({
      ivConfidence: {
        ivSource: "recorder", latestConfidence: 0.9, floor: 0.1, gatedNow: false, nGated: 0, healthImputed: false,
      },
    });

    expect(note()).toBeNull();
    expect(screen.getByTestId("iv-confidence-banner").textContent).toContain("VRP signal active");
  });

  it("does not claim an active VRP signal in the confidence banner when the window has no z-score", async () => {
    await show({
      vrpZ: none(12),
      coverage: { forwardBlindTail: 3, vrpLookbackBars: 11, ivFirstTsMs: null },
      ivConfidence: {
        ivSource: "recorder", latestConfidence: 0.9, floor: 0.1, gatedNow: false, nGated: 0, healthImputed: false,
      },
    });

    const banner = screen.getByTestId("iv-confidence-banner").textContent ?? "";
    expect(banner).toContain("Latest bar above floor.");
    expect(banner).not.toContain("VRP signal active");
  });
});

describe("RealizedVsIvComponent — failed request", () => {
  it("shows the error and no numbers", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await failCompute(world, "Polygon is unavailable");
    fixture.detectChanges();

    expect(screen.getByTestId("rviv-error").textContent).toContain("Polygon is unavailable");
    expect(chartCount(), "no chart on error").toBe(0);
    expect(screen.queryByTestId("rviv-readout")).toBeNull();
    expect(screen.queryByTestId("rviv-source-tag")).toBeNull();
    expect(screen.queryByTestId("sample-data-banner")).toBeNull();
  });

  it("drops an earlier result when a later Compute fails, so it never reads as this request's answer", async () => {
    const { world, user, fixture } = await renderPage();
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world);
    fixture.detectChanges();
    expect(screen.getByTestId("rviv-readout")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await tick();
    world.jobs.fail("internal_error", "Polygon is unavailable");
    await tick();
    fixture.detectChanges();

    expect(screen.getByTestId("rviv-error")).toBeTruthy();
    expect(chartCount()).toBe(0);
    expect(screen.queryByTestId("rviv-readout")).toBeNull();
  });
});

/**
 * Banner-rendering tests for the IV-source/confidence panel added in
 * the Step E + recorder-fallback follow-up. Asserts on the rendered
 * DOM (data-testid hooks), not on private signal values.
 */
describe("RealizedVsIvComponent — IV confidence banner", () => {
  async function renderWithConfidence(iv: IvConfidenceSummary | null): Promise<void> {
    const { fixture } = await renderPage();
    fixture.componentInstance.data.set(realData({ ivConfidence: iv }));
    fixture.detectChanges();
  }

  function banner(): HTMLElement | null {
    return screen.queryByTestId("iv-confidence-banner");
  }

  function bannerOrThrow(): HTMLElement {
    const el = banner();
    if (!el) throw new Error("iv-confidence-banner not rendered");
    return el;
  }

  function textOf(root: HTMLElement, selector: string): string {
    const node = root.querySelector(selector);
    if (!node) throw new Error(`selector ${selector} matched nothing`);
    return node.textContent ?? "";
  }

  it("does not render the banner when ivConfidence is null", async () => {
    await renderWithConfidence(null);
    expect(banner()).toBeNull();
  });

  it("renders the recorder-fallback label and confidence, and no imputed pill, for a recorder IV with real evidence", async () => {
    await renderWithConfidence({
      ivSource: "recorder",
      latestConfidence: 0.83,
      floor: 0.5,
      gatedNow: false,
      nGated: 0,
      healthImputed: false,
    });

    const el = bannerOrThrow();
    expect.soft(el.classList.contains("banner-iv-confidence-warn"), "stays off the warn variant").toBe(false);
    expect
      .soft(textOf(el, '[data-testid="iv-source"]'), "renders the recorder-fallback label when iv_source=recorder")
      .toContain("recorder fallback");
    expect
      .soft(textOf(el, '[data-testid="iv-confidence-value"]'), "renders the confidence when iv_source=recorder")
      .toContain("83%");
    expect
      .soft(el.querySelector('[data-testid="iv-confidence-imputed"]'), "hides the imputed pill when healthImputed=false (real evidence)")
      .toBeNull();
  });

  it("flips to the warn variant and surfaces the gated-now copy when gatedNow=true", async () => {
    await renderWithConfidence({
      ivSource: "caller_supplied",
      latestConfidence: 0.32,
      floor: 0.5,
      gatedNow: true,
      nGated: 4,
      healthImputed: false,
    });

    const el = bannerOrThrow();
    expect(el.classList.contains("banner-iv-confidence-warn")).toBe(true);
    expect(el.querySelector('[data-testid="iv-gated-now"]')).not.toBeNull();
    expect(textOf(el, '[data-testid="iv-n-gated"]')).toContain("4 bars");
  });

  it("uses singular bar wording when exactly one bar is gated", async () => {
    await renderWithConfidence({
      ivSource: "recorder",
      latestConfidence: 0.6,
      floor: 0.5,
      gatedNow: false,
      nGated: 1,
      healthImputed: false,
    });

    const text = textOf(bannerOrThrow(), '[data-testid="iv-n-gated"]');
    expect(text).toContain("1 bar");
    // Make sure it's not the plural "bars" form.
    expect(text).not.toContain("bars");
  });

  it("hides the gated-now copy, the confidence value and the imputed pill when confidence wasn't computed", async () => {
    await renderWithConfidence({
      ivSource: "absent",
      latestConfidence: null,
      floor: null,
      gatedNow: null,
      nGated: 0,
      healthImputed: null,
    });

    const el = bannerOrThrow();
    expect
      .soft(el.querySelector('[data-testid="iv-gated-now"]'), "hides the gated-now copy when confidence wasn't computed (gatedNow=null)")
      .toBeNull();
    expect
      .soft(el.querySelector('[data-testid="iv-confidence-value"]'), "hides the confidence value when confidence wasn't computed")
      .toBeNull();
    expect
      .soft(textOf(el, '[data-testid="iv-source"]'), "says no IV was provided when ivSource is absent")
      .toContain("no IV provided");
    expect
      .soft(el.querySelector('[data-testid="iv-confidence-imputed"]'), "hides the imputed pill when healthImputed=null (confidence not computed)")
      .toBeNull();
  });

  // ADR 0071 decision 11: when the recorder row lacks an explicit
  // health_score, confidence drops the health factor (1 - vcs); the UI
  // flags this so the consumer doesn't treat the confidence number as
  // evidence-backed.

  it("renders the imputed pill when healthImputed=true", async () => {
    await renderWithConfidence({
      ivSource: "recorder",
      latestConfidence: 0.6,
      floor: 0.5,
      gatedNow: false,
      nGated: 0,
      healthImputed: true,
    });

    const pill = bannerOrThrow().querySelector('[data-testid="iv-confidence-imputed"]');
    expect(pill).not.toBeNull();
    expect(pill?.textContent).toContain("imputed");
    // Accessible name surfaces the rationale for screen-reader users.
    expect(pill?.getAttribute("aria-label")).toContain("imputed");
  });
});

describe("RealizedVsIvComponent — live IV30 readout", () => {
  async function renderWithLive(live: LiveIv30State): Promise<void> {
    const { fixture } = await renderPage();
    fixture.componentInstance.data.set(realData({ liveIv30: live }));
    fixture.detectChanges();
  }

  it("says the live_iv30 row is unavailable, and why, when neither endpoint answered", async () => {
    await renderWithLive({ kind: "unavailable", reason: "Neither live IV30 endpoint answered. vix-style: 503 the chain is dark; parametric: 503 the chain is dark." });

    expect(screen.queryByTestId("live-iv30-readout")).toBeNull();
    expect(screen.getByTestId("live-iv30-unavailable").textContent).toBe("unavailable");
    const reason = screen.getByTestId("live-iv30-reason").textContent ?? "";
    expect(reason).toContain("vix-style: 503 the chain is dark");
    expect(reason).toContain("parametric: 503 the chain is dark");
  });

  it("renders the live IV30 percent, the vix-style method label and when it was taken", async () => {
    await renderWithLive({
      kind: "available",
      marker: {
        method: "vix_style",
        iv30Act365: 0.213,
        snapshotTsMs: 1_700_000_000_000,
        spot: 591,
        varianceContributionSynthetic: 0.04,
        strikeCoverageScore: 0.92,
      },
    });

    const readout = screen.getByTestId("live-iv30-readout");
    expect(readout.textContent).toContain("21.3%");
    expect(screen.getByTestId("live-iv30-method").textContent).toContain("vix-style");
    expect(screen.getByTestId("live-iv30-synth").textContent).toContain("4%");
    expect(screen.getByTestId("live-iv30-asof").textContent).toContain("as of");
    expect(screen.queryByTestId("live-iv30-unavailable")).toBeNull();
  });

  it("labels the parametric fallback when method=parametric", async () => {
    await renderWithLive({
      kind: "available",
      marker: {
        method: "parametric",
        iv30Act365: 0.18,
        snapshotTsMs: 1_700_000_000_000,
        spot: 591,
        varianceContributionSynthetic: 0.0,
        strikeCoverageScore: 0.5,
      },
    });

    const method = screen.getByTestId("live-iv30-method");
    expect(method.textContent).toContain("parametric");
    expect(method.textContent).not.toContain("vix-style");
  });
});

describe("RealizedVsIvComponent — rerun from a saved run", () => {
  const recordUrl = `${environment.pythonServiceUrl}/api/research/study-records/realized-vs-iv/${RECORD_ID}`;

  async function rerunWith(inputs: Record<string, unknown>) {
    const page = await renderPage({ rerun: RECORD_ID });
    page.world.http.expectOne(recordUrl).flush(storedRecord("realized-vs-iv", { inputs: { ...RVIV_INPUTS, ...inputs } as never }));
    await tick();
    await settle(page.fixture);
    return page;
  }

  const pressedSession = () => {
    const group = screen.queryByTestId("rviv-session");
    if (group === null) return null;
    return ["RTH", "Ext"].find((name) => within(group).getByRole("button", { name }).getAttribute("aria-pressed") === "true") ?? null;
  };

  it("fills the inputs from the saved run and starts nothing, then names the run when Run study is pressed", async () => {
    const { world, user } = await rerunWith({ symbol: "qqq", bar_size: "15m", tenor_days: 60, session: "rth", hf_session: "RTH" });

    expect((screen.getByLabelText("Bar size") as HTMLSelectElement).value).toBe("15m");
    expect((screen.getByLabelText("Tenor") as HTMLSelectElement).value).toBe("60D");
    expect(pressedSession()).toBe("RTH");
    expect(screen.getByTestId("study-prefill").textContent).toContain("Nothing has run");
    expect(world.jobs.started).toHaveLength(0);

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await tick();

    expect(world.jobs.started[0].payload).toMatchObject({
      symbol: "QQQ", bar_size: "15m", tenor_days: 60, session: "rth", hf_session: "RTH", rerun_of: RECORD_ID,
    });
  });

  it("fills extended hours for a 15-minute run that names its session extended", async () => {
    await rerunWith({ bar_size: "15m", session: "extended", hf_session: "ETH" });

    expect(pressedSession()).toBe("Ext");
  });

  it("fills extended hours for a 15-minute run saved before runs named their session, which read extended hours", async () => {
    await rerunWith({ bar_size: "15m", session: null, hf_session: "ETH" });

    expect(pressedSession()).toBe("Ext");
  });

  it("fills a daily run, which has no session control, and runs it on regular hours", async () => {
    const { world, user } = await rerunWith({ bar_size: "1d", session: null, hf_session: "ETH" });

    expect(pressedSession(), "daily bars have no session control").toBeNull();
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await tick();
    expect(world.jobs.started[0].payload).toMatchObject({ bar_size: "1d", session: "rth" });
  });

  it.each([
    ["extended bars with the HF estimator on RTH, which the one session control cannot say", { bar_size: "15m", session: null, hf_session: "RTH" }, /HF estimator on RTH/],
    ["regular-hours bars with the HF estimator on ETH", { bar_size: "15m", session: "rth", hf_session: "ETH" }, /HF estimator on ETH/],
    ["a daily run on extended hours", { bar_size: "1d", session: "extended" }, /daily bars read regular hours only/],
    ["a session the page does not offer", { bar_size: "15m", session: "overnight", hf_session: "ETH" }, /not a session this page offers/],
  ])("fills nothing for %s, and says why", async (_name, inputs, reason) => {
    await rerunWith({ symbol: "qqq", tenor_days: 60, ...inputs });

    expect(screen.getByTestId("study-prefill").textContent).toMatch(reason);
    expect((screen.getByLabelText("Tenor") as HTMLSelectElement).value, "the page is as it was").toBe("30D");
  });
});

describe("RealizedVsIvComponent — the window a run reads", () => {
  const windowText = () => screen.getByTestId("rviv-window").textContent?.replace(/\s+/g, " ").trim();

  it("asks the server which window a run reads, for the bar size and session on screen, and shows the answer", async () => {
    const { world, user, fixture } = await renderPage();
    await settle(fixture);
    expect(screen.getByTestId("rviv-window-reading")).toBeTruthy();

    const daily = takeWindowRequest(world.http);
    expect(daily).toMatchObject({ bar_size: "1d", session: "rth" });
    daily.answer();
    await settle(fixture);

    expect(windowText()).toContain("2023-11-29 to 2024-12-30");
    expect(screen.getByTestId("rviv-window-note").textContent).toBe("273 sessions, regular hours");

    await user.selectOptions(screen.getByTestId("rviv-bar-size"), "15m");
    await settle(fixture);
    expect(takeWindowRequest(world.http)).toMatchObject({ bar_size: "15m", session: "extended" });
  });

  it("asks again, for regular hours, when the session is changed, and shows that window", async () => {
    const { world, user, fixture } = await renderPage();
    await user.selectOptions(screen.getByTestId("rviv-bar-size"), "15m");
    await settle(fixture);
    takeWindowRequest(world.http).answer({ sessions: 25 });
    await settle(fixture);
    expect(screen.getByTestId("rviv-window-note").textContent).toBe("25 sessions, extended hours");

    await user.click(within(screen.getByTestId("rviv-session")).getByRole("button", { name: "RTH" }));
    await settle(fixture);
    const regular = takeWindowRequest(world.http);
    expect(regular).toMatchObject({ bar_size: "15m", session: "rth" });
    regular.answer({ sessions: 31 });
    await settle(fixture);

    expect(screen.getByTestId("rviv-window-note").textContent).toBe("31 sessions, regular hours");
  });

  it("shows no window for a bar size or session the answer was not for, while the new one is read", async () => {
    const { world, user, fixture } = await renderPage();
    await answerWindow(world.http);
    fixture.detectChanges();
    expect(screen.queryByTestId("rviv-window-note")).toBeTruthy();

    await user.selectOptions(screen.getByTestId("rviv-bar-size"), "15m");
    fixture.detectChanges();

    expect(screen.queryByTestId("rviv-window-note"), "the daily window is not the 15-minute run's").toBeNull();
    expect(screen.getByTestId("rviv-window-reading")).toBeTruthy();
  });

  it("sends the window it showed with the run, so the run reads exactly the span that was counted", async () => {
    const { world, user, fixture } = await renderPage();
    await answerWindow(world.http);
    fixture.detectChanges();

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await tick();

    expect(world.jobs.started[0].payload).toMatchObject({
      window_start_ms: Date.UTC(2023, 10, 29, 5), window_end_ms: Date.UTC(2024, 11, 30, 5),
    });
  });

  it("says when the window cannot be read, and still runs, leaving the server to pick it", async () => {
    const { world, user, fixture } = await renderPage();
    await settle(fixture);
    takeWindowRequest(world.http).fail({ detail: "the calendar is down" }, 503);
    await settle(fixture);

    expect(screen.getByTestId("rviv-window-error").textContent).toContain("503 the calendar is down");

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await tick();
    expect(world.jobs.started[0].payload).not.toHaveProperty("window_start_ms");
    expect(world.jobs.started[0].payload).not.toHaveProperty("window_end_ms");
  });

  it("marks a result as previous once the window it answered is not the one on screen", async () => {
    const { world, user, fixture } = await renderPage();
    await answerWindow(world.http);
    fixture.detectChanges();
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world);
    fixture.detectChanges();
    expect(screen.queryByText("Previous result")).toBeNull();

    await user.selectOptions(screen.getByTestId("rviv-bar-size"), "15m");
    await user.selectOptions(screen.getByTestId("rviv-bar-size"), "1D");
    await settle(fixture);
    await answerWindow(world.http, { window_end_ms: Date.UTC(2024, 11, 31, 5), sessions: 274 });
    fixture.detectChanges();

    const banner = screen.getByText("Previous result").closest("div");
    if (banner === null) throw new Error("no previous-result banner");
    expect(within(banner).getByText("2023-11-29 to 2024-12-30"), "the window it was computed over").toBeTruthy();
  });
});

describe("RealizedVsIvComponent — sessions the lake does not hold yet", () => {
  const lacking = (missing: number) => ({ kind: "ok" as const, value: heldReport({ sessions: 273, missing }) });
  const warning = () => screen.queryByText(/in the lake yet/);

  async function renderWithWindow(read: DataLakeRead<InstrumentDataReport>) {
    const page = await renderPage();
    page.instrumentData.mockResolvedValue(read);
    await answerWindow(page.world.http);
    page.fixture.detectChanges();
    await vi.waitFor(() => expect(page.instrumentData).toHaveBeenCalledTimes(1));
    await settle(page.fixture);
    return page;
  }

  it("counts the sessions of the window the server named, for the instrument on screen, in the split-adjusted tree", async () => {
    const { instrumentData } = await renderWithWindow(lacking(0));

    expect(instrumentData).toHaveBeenCalledWith({ symbol: "SPY", from: "2023-11-29", to: "2024-12-30", tree: "polygon_split_adjusted" });
    expect(warning(), "the lake holds it all, so the page says nothing").toBeNull();
  });

  it("reads nothing until the server has named the window: the browser works out no span of its own", async () => {
    const { instrumentData, fixture } = await renderPage();
    await settle(fixture);

    expect(instrumentData).not.toHaveBeenCalled();
  });

  it("warns how many of the run’s sessions the lake lacks and why the first run is slow, with an action to capture them", async () => {
    await renderWithWindow(lacking(12));

    expect(warning()?.textContent).toBe("12 of 273 sessions aren’t in the lake yet.");
    expect(screen.getByText("The first run fetches them one session at a time, so it will be slow.")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Capture 12 sessions" })).toBeTruthy();
  });

  it("starts no capture by itself, and does not hold up Run study", async () => {
    const { world, user, captureRun } = await renderWithWindow(lacking(12));

    expect(captureRun).not.toHaveBeenCalled();
    expect((screen.getByRole("button", { name: "Run study" }) as HTMLButtonElement).disabled).toBe(false);
    await user.click(screen.getByRole("button", { name: "Run study" }));
    await tick();
    expect(world.jobs.started).toHaveLength(1);
  });

  it("counts again for the next window when the bar size or session changes it, and again when a run ends", async () => {
    const { world, user, fixture, instrumentData } = await renderWithWindow(lacking(12));

    await user.selectOptions(screen.getByTestId("rviv-bar-size"), "15m");
    await settle(fixture);
    await answerWindow(world.http, { window_start_ms: Date.UTC(2024, 10, 22, 5), sessions: 25 });
    fixture.detectChanges();
    await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(2));
    expect(instrumentData).toHaveBeenLastCalledWith({ symbol: "SPY", from: "2024-11-22", to: "2024-12-30", tree: "polygon_split_adjusted" });

    await user.click(screen.getByRole("button", { name: "Run study" }));
    await answerCompute(world, { run: { bar_size: "15m", tenor_days: 30, hf_session: "ETH" } });
    await vi.waitFor(() => expect(instrumentData).toHaveBeenCalledTimes(3));
  });

  it("says the count could not be read, and that Run study still works", async () => {
    await renderWithWindow({ kind: "unavailable", reason: "network", message: "offline" } as never);

    expect(screen.getByText("Could not check which of this run’s sessions the lake holds.")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Run study" }) as HTMLButtonElement).disabled).toBe(false);
  });
});
