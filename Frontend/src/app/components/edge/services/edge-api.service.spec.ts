import { TestBed } from "@angular/core/testing";
import { vi } from "vitest";
import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { JobsService } from "../../../services/jobs.service";
import {
  answerCompute, answerLiveIv, FakeJobs, failCompute, refuseCompute, resultResponse, tick, YZ_RV_BY_WINDOW,
  type ComputeWorld,
} from "../testing/edge-compute-world";
import { EdgeApiService, LakeStudyRefusedError } from "./edge-api.service";

function configure(): { service: EdgeApiService; world: ComputeWorld } {
  TestBed.resetTestingModule();
  const jobs = new FakeJobs();
  TestBed.configureTestingModule({
    providers: [
      EdgeApiService,
      { provide: JobsService, useValue: jobs },
      provideHttpClient(),
      provideHttpClientTesting(),
    ],
  });
  return { service: TestBed.inject(EdgeApiService), world: { http: TestBed.inject(HttpTestingController), jobs } };
}

/** Targeted tests for `getLiveIv30` — the live-IV30 fetch with
 *  vix-style → parametric fallback. Here we pin the fallback policy, the
 *  response→marker mapping and what is said when neither endpoint answers; the
 *  compute flow has its own describe below. */
describe("EdgeApiService.getLiveIv30", () => {
  let service: EdgeApiService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    ({ service, world: { http: httpMock } } = configure());
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

  function available(state: Awaited<ReturnType<EdgeApiService["getLiveIv30"]>>) {
    if (state.kind !== "available") throw new Error(`expected a marker, got: ${state.reason}`);
    return state.marker;
  }

  it("returns the vix-style marker on the happy path", async () => {
    const promise = service.getLiveIv30("SPY");
    const req = httpMock.expectOne("/api/edge/iv30/vix-style");
    expect(req.request.method).toBe("POST");
    expect(req.request.body).toEqual({ symbol: "SPY", target_calendar_days: 30 });
    req.flush(mockIv30Response("vix_style", 0.21));

    const marker = available(await promise);
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

    const marker = available(await promise);
    expect(marker.method).toBe("parametric");
    expect(marker.iv30Act365).toBeCloseTo(0.18, 6);
  });

  it("says it is unavailable, and why from each endpoint, when both error", async () => {
    const promise = service.getLiveIv30("SPY");
    httpMock.expectOne("/api/edge/iv30/vix-style").flush(
      { detail: "polygon outage" }, { status: 502, statusText: "Bad Gateway" });
    await Promise.resolve(); await Promise.resolve();
    httpMock.expectOne("/api/edge/iv30/parametric").flush(
      { detail: "no straddle in the chain" }, { status: 422, statusText: "Unprocessable" });

    const state = await promise;
    expect(state.kind).toBe("unavailable");
    if (state.kind !== "unavailable") return;
    expect(state.reason, "names the endpoint that failed first and the server's own reason").toContain("vix-style: 502 polygon outage");
    expect(state.reason, "and the one it fell back to").toContain("parametric: 422 no straddle in the chain");
  });

  it("reports a data service that did not answer at all as that, not as an empty market", async () => {
    const promise = service.getLiveIv30("SPY");
    httpMock.expectOne("/api/edge/iv30/vix-style").error(new ProgressEvent("error"));
    await Promise.resolve(); await Promise.resolve();
    httpMock.expectOne("/api/edge/iv30/parametric").error(new ProgressEvent("error"));

    const state = await promise;
    expect(state.kind === "unavailable" && state.reason).toContain("the data service did not answer");
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
  let world: ComputeWorld;

  const request = { symbol: "SPY", barSize: "1D", tenor: "30D", estimators: ["yz"] } as const;

  beforeEach(() => {
    ({ service, world } = configure());
  });

  afterEach(() => world.http.verify());

  it("starts the study on the lake with the bar size and tenor it was asked for, and sends no bars", async () => {
    const promise = service.computeRealizedVsIv({ ...request, barSize: "15m", tenor: "60D", hfSession: "RTH" });
    await answerCompute(world, { run: { bar_size: "15m", tenor_days: 60, hf_session: "RTH" } });
    await promise;

    expect(world.jobs.started).toEqual([
      {
        type: "realized_vs_iv",
        payload: { symbol: "SPY", bar_size: "15m", tenor_days: 60, hf_session: "RTH", estimators: ["yz"], windows: [5, 10, 30] },
      },
    ]);
    world.http.expectNone("/api/aggregates/fetch");
  });

  it("sends the daily size as 1d, never as another size, and no HF session, which a daily run has no estimator for", async () => {
    const promise = service.computeRealizedVsIv({ ...request, hfSession: "RTH" });
    await answerCompute(world);
    await promise;

    expect(world.jobs.started[0].payload["bar_size"]).toBe("1d");
    expect(world.jobs.started[0].payload).not.toHaveProperty("hf_session");
  });

  it("returns a dataset that says it is real, with missing samples as NaN, the settings the run used, and the lake it read", async () => {
    const promise = service.computeRealizedVsIv(request);
    await answerCompute(world);

    const result = await promise;
    expect.soft(result.provenance).toBe("real");
    expect.soft(result.symbol).toBe("SPY");
    expect.soft(result.N).toBe(40);
    expect.soft(result.iv30[0], "no IV before the first sample").toBeNaN();
    expect.soft(result.iv30[5]).toBeCloseTo(0.21, 12);
    expect.soft(result.coverage.forwardBlindTail).toBe(3);
    expect.soft(result.coverage.ivFirstTsMs).toBe(result.dates[5].getTime());
    expect.soft(result.candles[0]).toEqual({ o: 500, h: 503, l: 498, c: 501, v: 1_000_000 });
    expect.soft(result.run).toEqual({ barSize: "1D", tenorDays: 30, hfSession: null });
    expect.soft(result.dataSource.coverage?.window).toMatchObject({ added: 10, reused: 30, missing: 0 });
    expect.soft(result.dataSource.barMinutes).toBe(1440);
    expect.soft(result.ivAlignment).toEqual({ rule: "snapshot_in_bar_latest", snapshotsRead: 35, barsWithIv: 35, superseded: 0, outsideBars: 0 });
  });

  it("picks each Yang-Zhang window by key, whatever other estimators the response lists first", async () => {
    const promise = service.computeRealizedVsIv({ ...request, estimators: ["ctc", "yz"] });
    await answerCompute(world, { estimators: ["ctc", "yz"] });

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
    await answerCompute(world, { estimators: ["ctc"] });
    await rejection;
  });

  it("never puts the current IV on a past date: the live marker arrives beside an IV series that has none", async () => {
    const promise = service.computeRealizedVsIv(request);
    await answerCompute(world, { ivPresent: false, live: "vix" });

    const result = await promise;
    expect(result.liveIv30.kind).toBe("available");
    expect(result.liveIv30.kind === "available" && result.liveIv30.marker.iv30Act365).toBeCloseTo(0.213, 12);
    expect(result.iv30.every((v) => Number.isNaN(v)), "no bar was given the live value").toBe(true);
    expect(result.iv30Trd252.every((v) => Number.isNaN(v))).toBe(true);
    expect(result.vrpForward.every((v) => Number.isNaN(v))).toBe(true);
  });

  it("says why there is no current IV when both live endpoints are dark, and still returns the study", async () => {
    const promise = service.computeRealizedVsIv(request);
    await answerCompute(world, { live: "dark" });

    const result = await promise;
    expect(result.liveIv30.kind === "unavailable" && result.liveIv30.reason).toContain("the chain is dark");
    expect(result.N).toBe(40);
  });

  it("hands the job's log lines to the caller as they arrive", async () => {
    const lines: string[] = [];
    const promise = service.computeRealizedVsIv(request, (line) => lines.push(line));
    await answerCompute(world);
    await promise;

    expect(lines).toEqual(["Checking the lake for SPY daily bars", "Loaded 40 bars from 15,600 minute bars"]);
  });

  it("settles on a job that finished before the listener attached", async () => {
    const promise = service.computeRealizedVsIv(request);
    world.jobs.complete(resultResponse());
    await tick();
    await answerLiveIv(world.http);

    expect((await promise).N).toBe(40);
  });

  it("rejects with the server's reason and the admission counts when the lake could not supply the bars", async () => {
    const refusal = {
      success: false, status: "refused", symbol: "SPY", error: "The lake cannot admit this window.",
      refusal: {
        code: "data_unavailable_from_provider", reason: "The lake cannot admit this window: 1 of 252 sessions cannot be supplied by the provider.",
        sessions: [{ group: "window", trading_date_ms: Date.UTC(2025, 5, 4, 13, 30), outcome: "unavailable_from_provider", reason: "provider_no_data", failure_reason: "provider_no_data" }],
        sessions_total: 1,
        coverage: {
          window: { expected_sessions: 252, reused: 200, added: 51, missing: 0, failed: 1, building: 0, corrupt: 0, unavailable_from_provider: 1 },
          warmup: { expected_sessions: 0, reused: 0, added: 0, missing: 0, failed: 0, building: 0, corrupt: 0, unavailable_from_provider: 0 },
          artifacts: { fetched: 51, reused: 200, refreshed: 0 },
        },
      },
      data_source: null,
    };
    const rejection = service.computeRealizedVsIv(request).catch((error: unknown) => error);
    await refuseCompute(world, refusal);

    const error = await rejection;
    expect(error).toBeInstanceOf(LakeStudyRefusedError);
    if (!(error instanceof LakeStudyRefusedError)) return;
    expect(error.message).toContain("1 of 252 sessions cannot be supplied");
    expect(error.refused.refusal.coverage?.window).toMatchObject({ reused: 200, added: 51, failed: 1 });
  });

  it("rejects with the job's own message when it failed for another reason and parked nothing", async () => {
    const rejection = expect(service.computeRealizedVsIv(request)).rejects.toThrow("the series needs at least two bars");
    await failCompute(world, "the series needs at least two bars, got 1");
    await rejection;
  });

  it("does not swallow a failure to read the parked result that is not 'there is none'", async () => {
    const parked = vi.spyOn(world.jobs, "fetchResult").mockRejectedValue(new Error("result store unreachable"));
    const rejection = expect(service.computeRealizedVsIv(request)).rejects.toThrow("result store unreachable");
    await tick();
    world.jobs.fail("data_missing", "the lake is missing sessions");
    await answerLiveIv(world.http);
    await rejection;
    expect(parked).toHaveBeenCalled();
  });

  it("rejects when the job is cancelled", async () => {
    const rejection = expect(service.computeRealizedVsIv(request)).rejects.toThrow("cancelled by the operator");
    await tick();
    world.jobs.emit({ type: "job.cancelled", reason: "cancelled by the operator" });
    await answerLiveIv(world.http);
    await rejection;
  });
});
