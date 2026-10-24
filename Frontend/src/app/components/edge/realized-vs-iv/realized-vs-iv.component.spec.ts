import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { TestBed } from "@angular/core/testing";
import { provideRouter } from "@angular/router";
import { render, screen, within } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { JobsService } from "../../../services/jobs.service";
import { fakePickerWorld } from "../../../shared/symbol-picker/testing/fake-picker-world";
import type {
  IvConfidenceSummary,
  LiveIv30State,
  RealizedVsIvData,
} from "../services/edge-data.model";
import {
  answerCompute, failCompute, FakeJobs, lakeDataSource, refuseCompute, tick, YZ_RV_BY_WINDOW, type ComputeWorld,
} from "../testing/edge-compute-world";
import { mapLakeStudyDataSource } from "../../../shared/lake-study/lake-study";
import { RealizedVsIvComponent } from "./realized-vs-iv.component";

async function renderPage() {
  const jobs = new FakeJobs();
  const view = await render(RealizedVsIvComponent, {
    providers: [
      ...fakePickerWorld().providers,
      { provide: JobsService, useValue: jobs },
      provideHttpClient(),
      provideHttpClientTesting(),
      provideRouter([]),
    ],
  });
  const world: ComputeWorld = { http: TestBed.inject(HttpTestingController), jobs };
  return { ...view, world, user: userEvent.setup() };
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

describe("RealizedVsIvComponent — opens idle", () => {
  it("shows no study, no chart and no sample label before anything is computed", async () => {
    await renderPage();

    expect(screen.getByTestId("rviv-idle")).toBeTruthy();
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
    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(world, { run: { bar_size: "15m", tenor_days: 60, hf_session: "ETH" } });
    fixture.detectChanges();

    expect(world.jobs.started[0].payload).toMatchObject({ bar_size: "15m", tenor_days: 60, hf_session: "ETH" });
    expect(select("rviv-bar-size").value).toBe("15m");
    expect(select("rviv-tenor").value).toBe("60D");
    expect(screen.getByTestId("rviv-run-settings").textContent?.replace(/\s+/g, " ").trim()).toBe("15m bars · 60D tenor · HF ETH");
  });

  it("says which settings produced the result on screen even after a control is changed without computing again", async () => {
    const { world, user, fixture } = await renderPage();
    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(world);
    fixture.detectChanges();

    await user.selectOptions(select("rviv-bar-size"), "15m");
    fixture.detectChanges();

    expect(screen.getByTestId("rviv-run-settings").textContent?.replace(/\s+/g, " ").trim()).toBe("1D bars · 30D tenor");
  });

  it("offers the HF session for 15-minute bars only, since a daily run has no HF estimator", async () => {
    const { user } = await renderPage();
    expect(screen.queryByTestId("rviv-hf-session"), "daily is the opening size").toBeNull();

    await user.selectOptions(select("rviv-bar-size"), "15m");
    expect(screen.getByTestId("rviv-hf-session")).toBeTruthy();

    await user.selectOptions(select("rviv-bar-size"), "1D");
    expect(screen.queryByTestId("rviv-hf-session")).toBeNull();
  });

  it("sends the HF session the user chose with a 15-minute run", async () => {
    const { world, user } = await renderPage();

    await user.selectOptions(select("rviv-bar-size"), "15m");
    await user.click(within(screen.getByTestId("rviv-hf-session")).getByRole("button", { name: "RTH" }));
    await user.click(screen.getByRole("button", { name: "Compute" }));
    await tick();

    expect(world.jobs.started[0].payload).toMatchObject({ bar_size: "15m", hf_session: "RTH" });
  });
});

describe("RealizedVsIvComponent — real response", () => {
  it("draws the returned study and reads edge score as unavailable, not zero", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Compute" }));
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

    await user.click(screen.getByRole("button", { name: "Compute" }));
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
    await user.click(screen.getByRole("button", { name: "Compute" }));
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

    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(world);
    fixture.detectChanges();

    expect(screen.getByText(/Forward RV unavailable for last 3 bars/)).toBeTruthy();
  });
});

describe("RealizedVsIvComponent — where the bars came from", () => {
  it("prints the data-source line with the lake, the window, the resolution and the added, reused and missing counts", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(world);
    fixture.detectChanges();

    const summary = screen.getByText("Data source").closest("summary");
    expect(summary?.textContent).toContain("Data lake, Polygon");
    expect(summary?.textContent).toContain("daily bars, folded from 1-minute bars");
    expect(screen.getByText("10 added · 30 reused · 0 missing")).toBeTruthy();
  });

  it("names the IV input as not from the lake, with how many snapshots it read and their fingerprint", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(world);
    fixture.detectChanges();

    const note = screen.getByTestId("rviv-iv-input").textContent?.replace(/\s+/g, " ") ?? "";
    expect(note).toContain("IV30 input, not from the lake");
    expect(note).toContain("IV recorder's file store, 35 snapshots in the window");
    expect(note).toContain("fingerprint cccccccccccc");
  });

  it("says there is no IV input at all when nothing was recorded or supplied, rather than naming a source", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Compute" }));
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

    await user.click(screen.getByRole("button", { name: "Compute" }));
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

    await user.click(screen.getByRole("button", { name: "Compute" }));
    await refuseCompute(world, {
      success: false, status: "refused", symbol: "SPY", error: reason,
      refusal: { code: "resolution_unsupported", reason, sessions: [], sessions_total: 0, coverage: null }, data_source: null,
    });
    fixture.detectChanges();

    expect(screen.getByTestId("rviv-refused").textContent).toContain(reason);
    expect(screen.queryByText("Data source"), "nothing was read, so there is no source to name").toBeNull();
  });
});

describe("RealizedVsIvComponent — no IV at these bars", () => {
  it("says there is no IV30 when the response carries none, rather than drawing an IV line, and why", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(world, { ivPresent: false });
    fixture.detectChanges();

    expect(document.querySelector(".banner-iv-missing")).not.toBeNull();
    expect(screen.getByTestId("rviv-readout").textContent).toContain("—");
    expect(screen.getByTestId("iv-missing-reason").textContent).toContain("The IV recorder holds no snapshot for SPY in this window");
  });

  it("says when snapshots exist but none lands inside a bar, instead of claiming the recorder is empty", async () => {
    const { world, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Compute" }));
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

    await user.click(screen.getByRole("button", { name: "Compute" }));
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

    await user.click(screen.getByRole("button", { name: "Compute" }));
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

    await user.click(screen.getByRole("button", { name: "Compute" }));
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
    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(world);
    fixture.detectChanges();
    expect(screen.getByTestId("rviv-readout")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Compute" }));
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
