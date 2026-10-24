import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { TestBed } from "@angular/core/testing";
import { provideRouter } from "@angular/router";
import { render, screen } from "@testing-library/angular";
import userEvent from "@testing-library/user-event";
import { describe, expect, it } from "vitest";
import { fakePickerWorld } from "../../../shared/symbol-picker/testing/fake-picker-world";
import type {
  IvConfidenceSummary,
  LiveIv30Marker,
  RealizedVsIvData,
} from "../services/edge-data.model";
import { answerCompute, failBarsFetch, tick, YZ_RV_BY_WINDOW } from "../testing/edge-compute-world";
import { RealizedVsIvComponent } from "./realized-vs-iv.component";

async function renderPage() {
  const view = await render(RealizedVsIvComponent, {
    providers: [
      ...fakePickerWorld().providers,
      provideHttpClient(),
      provideHttpClientTesting(),
      provideRouter([]),
    ],
  });
  return { ...view, http: TestBed.inject(HttpTestingController), user: userEvent.setup() };
}

/** A real-shaped result, for states that are easier to set than to drive
 *  through three HTTP round trips. */
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
    coverage: { forwardBlindTail: 2, ivFirstTsMs: Date.UTC(2026, 0, 6, 14) },
    ivConfidence: null,
    liveIv30: null,
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
  });
});

describe("RealizedVsIvComponent — real response", () => {
  it("draws the returned study and reads edge score as unavailable, not zero", async () => {
    const { http, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(http);
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
    const { http, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(http);
    fixture.detectChanges();

    const key = screen.getByText("rv_yz_30_daily");
    expect(key.nextElementSibling?.textContent?.trim()).toBe(`${(YZ_RV_BY_WINDOW[30] * 100).toFixed(1)}%`);
    expect(screen.getByTestId("rviv-readout").textContent, "the 5-bar series is not the readout").not.toContain(
      `${(YZ_RV_BY_WINDOW[5] * 100).toFixed(1)}%`,
    );
  });

  it("offers no estimator chips, since the page computes Yang-Zhang alone", async () => {
    await renderPage();

    for (const name of ["CtC", "Parkinson", "GK"]) {
      expect(screen.queryByRole("button", { name }), name).toBeNull();
    }
  });

  it("says there is no IV30 when the response carries none, rather than drawing an IV line", async () => {
    const { http, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(http, { ivPresent: false });
    fixture.detectChanges();

    expect(document.querySelector(".banner-iv-missing")).not.toBeNull();
    expect(screen.getByTestId("rviv-readout").textContent).toContain("—");
  });
});

describe("RealizedVsIvComponent — failed request", () => {
  it("shows the error and no numbers", async () => {
    const { http, user, fixture } = await renderPage();

    await user.click(screen.getByRole("button", { name: "Compute" }));
    failBarsFetch(http, "Polygon is unavailable");
    await tick();
    fixture.detectChanges();

    expect(screen.getByTestId("rviv-error").textContent).toContain("Polygon is unavailable");
    expect(chartCount(), "no chart on error").toBe(0);
    expect(screen.queryByTestId("rviv-readout")).toBeNull();
    expect(screen.queryByTestId("rviv-source-tag")).toBeNull();
    expect(screen.queryByTestId("sample-data-banner")).toBeNull();
  });

  it("drops an earlier result when a later Compute fails, so it never reads as this request's answer", async () => {
    const { http, user, fixture } = await renderPage();
    await user.click(screen.getByRole("button", { name: "Compute" }));
    await answerCompute(http);
    fixture.detectChanges();
    expect(screen.getByTestId("rviv-readout")).toBeTruthy();

    await user.click(screen.getByRole("button", { name: "Compute" }));
    failBarsFetch(http, "Polygon is unavailable");
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
  async function renderWithLive(live: LiveIv30Marker | null): Promise<void> {
    const { fixture } = await renderPage();
    fixture.componentInstance.data.set(realData({ liveIv30: live }));
    fixture.detectChanges();
  }

  it("does not render the live_iv30 readout row when liveIv30 is null", async () => {
    await renderWithLive(null);
    expect(screen.queryByTestId("live-iv30-readout")).toBeNull();
  });

  it("renders the live IV30 percent and the vix-style method label", async () => {
    await renderWithLive({
      method: "vix_style",
      iv30Act365: 0.213,
      snapshotTsMs: 1_700_000_000_000,
      spot: 591,
      varianceContributionSynthetic: 0.04,
      strikeCoverageScore: 0.92,
    });

    const readout = screen.getByTestId("live-iv30-readout");
    expect(readout.textContent).toContain("21.3%");
    expect(screen.getByTestId("live-iv30-method").textContent).toContain("vix-style");
    expect(screen.getByTestId("live-iv30-synth").textContent).toContain("4%");
  });

  it("labels the parametric fallback when method=parametric", async () => {
    await renderWithLive({
      method: "parametric",
      iv30Act365: 0.18,
      snapshotTsMs: 1_700_000_000_000,
      spot: 591,
      varianceContributionSynthetic: 0.0,
      strikeCoverageScore: 0.5,
    });

    const method = screen.getByTestId("live-iv30-method");
    expect(method.textContent).toContain("parametric");
    expect(method.textContent).not.toContain("vix-style");
  });
});
