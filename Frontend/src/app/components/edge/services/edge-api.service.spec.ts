import { TestBed } from "@angular/core/testing";
import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { answerCompute, tick, YZ_RV_BY_WINDOW } from "../testing/edge-compute-world";
import { EdgeApiService } from "./edge-api.service";

/** Targeted tests for `getLiveIv30` — the live-IV30 fetch with
 *  vix-style → parametric fallback. Here we pin the fallback policy and the
 *  response→marker mapping; the compute flow has its own describe below. */
describe("EdgeApiService.getLiveIv30", () => {
  let service: EdgeApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        EdgeApiService,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    service = TestBed.inject(EdgeApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  function mockIv30Response(method: "vix_style" | "parametric", iv30: number, vcs = 0.04) {
    return {
      symbol: "SPY",
      method,
      target_calendar_days: 30,
      iv30_act365: iv30,
      spot: 591.0,
      rate: 0.045,
      dividend_yield: 0.015,
      rate_source: "fred",
      dividend_source: "polygon",
      expiries_used_calendar_days: [28, 35],
      snapshot_ts_ms: 1_700_000_000_000,
      iv_provenance: {
        iv_source: method,
        price_source_mix: { opra_mid: 1.0 },
        variance_contribution_synthetic: vcs,
        strike_coverage_score: 0.92,
      },
    };
  }

  it("returns the vix-style marker on the happy path", async () => {
    const promise = service.getLiveIv30("SPY");
    const req = httpMock.expectOne("/api/edge/iv30/vix-style");
    expect(req.request.method).toBe("POST");
    expect(req.request.body).toEqual({ symbol: "SPY", target_calendar_days: 30 });
    req.flush(mockIv30Response("vix_style", 0.21));

    const marker = await promise;
    if (!marker) throw new Error("expected a marker");
    expect(marker.method).toBe("vix_style");
    expect(marker.iv30Act365).toBeCloseTo(0.21, 6);
    expect(marker.varianceContributionSynthetic).toBeCloseTo(0.04, 6);
  });

  it("falls back to parametric when vix-style errors", async () => {
    const promise = service.getLiveIv30("SPY");
    const vix = httpMock.expectOne("/api/edge/iv30/vix-style");
    vix.flush({ detail: "no straddle" }, { status: 422, statusText: "Unprocessable" });
    // The rejection→catch→retry runs on the microtask queue; let it settle
    // before the parametric request appears in the mock backend.
    await Promise.resolve(); await Promise.resolve();

    const param = httpMock.expectOne("/api/edge/iv30/parametric");
    param.flush(mockIv30Response("parametric", 0.18));

    const marker = await promise;
    if (!marker) throw new Error("expected a fallback marker");
    expect(marker.method).toBe("parametric");
    expect(marker.iv30Act365).toBeCloseTo(0.18, 6);
  });

  it("returns null silently when both endpoints error", async () => {
    const promise = service.getLiveIv30("SPY");
    httpMock.expectOne("/api/edge/iv30/vix-style").flush(
      { detail: "polygon outage" }, { status: 502, statusText: "Bad Gateway" });
    await Promise.resolve(); await Promise.resolve();
    httpMock.expectOne("/api/edge/iv30/parametric").flush(
      { detail: "polygon outage" }, { status: 502, statusText: "Bad Gateway" });

    expect(await promise).toBeNull();
  });

  it("forwards a non-default target_calendar_days", async () => {
    const promise = service.getLiveIv30("QQQ", 60);
    const req = httpMock.expectOne("/api/edge/iv30/vix-style");
    expect(req.request.body).toEqual({ symbol: "QQQ", target_calendar_days: 60 });
    req.flush(mockIv30Response("vix_style", 0.22));
    await promise;
  });
});

describe("EdgeApiService.computeRealizedVsIv", () => {
  let service: EdgeApiService;
  let httpMock: HttpTestingController;

  const request = { symbol: "SPY", barSize: "1D", tenor: "30D", estimators: ["yz"] } as const;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [EdgeApiService, provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(EdgeApiService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it("returns a dataset that says it is real, with missing samples as NaN and the live marker absent when its endpoints are dark", async () => {
    const promise = service.computeRealizedVsIv(request);
    await answerCompute(httpMock);

    const result = await promise;
    expect.soft(result.provenance).toBe("real");
    expect.soft(result.symbol).toBe("SPY");
    expect.soft(result.N).toBe(40);
    expect.soft(result.iv30[0], "no IV before the first sample").toBeNaN();
    expect.soft(result.iv30[5]).toBeCloseTo(0.21, 12);
    expect.soft(result.coverage.forwardBlindTail).toBe(3);
    expect.soft(result.coverage.ivFirstTsMs).toBe(result.dates[5].getTime());
    expect.soft(result.liveIv30).toBeNull();
  });

  it("picks each Yang-Zhang window by key, whatever other estimators the response lists first", async () => {
    const promise = service.computeRealizedVsIv({ ...request, estimators: ["ctc", "yz"] });
    await answerCompute(httpMock, { estimators: ["ctc", "yz"] });

    const result = await promise;
    expect.soft(result.rvYZ[10], "the readout series is yz_30").toBeCloseTo(YZ_RV_BY_WINDOW[30], 12);
    expect.soft(result.rvYzByWindow.map((s) => s.window)).toEqual([5, 10, 30]);
    expect.soft(result.rvYzByWindow.map((s) => s.values[10])).toEqual(
      [5, 10, 30].map((w) => YZ_RV_BY_WINDOW[w]),
    );
  });

  it("rejects, rather than reading another estimator's series, when the response has no yz_30", async () => {
    const rejection = expect(
      service.computeRealizedVsIv({ ...request, estimators: ["ctc"] }),
    ).rejects.toThrow("yz_30");
    await answerCompute(httpMock, { estimators: ["ctc"] });
    await rejection;
  });

  it("rejects with Python's reason when the bars fetch fails, leaving nothing to fall back to", async () => {
    const promise = service.computeRealizedVsIv(request);
    httpMock.expectOne("/api/aggregates/fetch").flush({
      success: false, ticker: "SPY", summary: {}, data: [], error: "Polygon is unavailable",
    });

    await expect(promise).rejects.toThrow("Polygon is unavailable");
  });

  it("rejects when the series request fails", async () => {
    const rejection = expect(service.computeRealizedVsIv(request)).rejects.toThrow();
    httpMock.expectOne("/api/aggregates/fetch").flush({
      success: true, ticker: "SPY", summary: {},
      data: [{ timestamp: 1_767_623_400_000, open: 1, high: 2, low: 1, close: 2, volume: 10 }],
    });
    await tick();
    httpMock.expectOne("/api/edge/realized-vs-iv/series")
      .flush({ detail: "boom" }, { status: 500, statusText: "Server Error" });
    httpMock.expectOne("/api/edge/iv30/vix-style")
      .flush({ detail: "dark" }, { status: 503, statusText: "Unavailable" });
    await tick();
    httpMock.expectOne("/api/edge/iv30/parametric")
      .flush({ detail: "dark" }, { status: 503, statusText: "Unavailable" });

    await rejection;
  });
});
