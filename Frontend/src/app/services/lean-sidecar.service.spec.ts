import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  LeanSidecarApiError,
  LeanSidecarService,
} from "./lean-sidecar.service";
import type { TrustedRunRequest, TrustedRunResponse } from "./lean-sidecar.types";

/**
 * Tests the launcher's `{detail: {reason, message}}` rejection envelope
 * round-trips into a typed `LeanSidecarApiError` so the component can
 * branch on `reason` without parsing free text. This is the contract
 * the data-plane router promises (see PR #249).
 */
describe("LeanSidecarService", () => {
  let service: LeanSidecarService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        LeanSidecarService,
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    service = TestBed.inject(LeanSidecarService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  const goodRequest: TrustedRunRequest = {
    run_id: "ut_run_001",
    symbol: "SPY",
    start_ms_utc: 1_736_121_600_000,
    end_ms_utc: 1_736_467_200_000,
    starting_cash: 100_000,
  };

  it("posts the trusted run request and returns the response", async () => {
    const fakeResponse: TrustedRunResponse = {
      run_id: "ut_run_001",
      is_clean: true,
      exit_code: 0,
      duration_ms: 1234,
      timed_out: false,
      lean_errors: {
        analysis_failed: [],
        failed_data_requests: [],
        runtime_error: [],
        other: [],
      },
      log_tail: "ok",
      manifest_path: "/tmp/manifest.json",
      workspace_root: "/tmp/ws",
      observations_path: "/tmp/obs.csv",
      lean_log_path: "/tmp/log.txt",
      normalized_path: "/tmp/normalized/result.json",
      normalized_parser_version: "phase-3a-r1",
      total_order_events: 2,
      total_equity_points: 30,
      strategy_execution_id: null,
    };

    const promise = service.startTrustedRun(goodRequest);
    const req = httpMock.expectOne((r) => r.method === "POST" && r.url.endsWith("/api/lean-sidecar/trusted-runs"));
    expect(req.request.body).toEqual(goodRequest);
    req.flush(fakeResponse);

    const result = await promise;
    expect(result.is_clean).toBe(true);
    expect(result.normalized_parser_version).toBe("phase-3a-r1");
  });

  it("turns the launcher 400 envelope into a typed LeanSidecarApiError", async () => {
    const promise = service.startTrustedRun(goodRequest).catch((e) => e);
    const req = httpMock.expectOne((r) => r.url.endsWith("/api/lean-sidecar/trusted-runs"));
    req.flush(
      { detail: { reason: "workspace_max_mb_exceeded", message: "over cap" } },
      { status: 400, statusText: "Bad Request" },
    );
    const err = await promise;
    expect(err).toBeInstanceOf(LeanSidecarApiError);
    expect((err as LeanSidecarApiError).status).toBe(400);
    expect((err as LeanSidecarApiError).reason).toBe("workspace_max_mb_exceeded");
    expect((err as LeanSidecarApiError).message).toBe("over cap");
  });

  it("maps a 503 with a launcher_unreachable reason through unchanged", async () => {
    const promise = service.startTrustedRun(goodRequest).catch((e) => e);
    const req = httpMock.expectOne((r) => r.url.endsWith("/api/lean-sidecar/trusted-runs"));
    req.flush(
      { detail: { reason: "launcher_unreachable", message: "connect refused" } },
      { status: 503, statusText: "Service Unavailable" },
    );
    const err = await promise;
    expect((err as LeanSidecarApiError).reason).toBe("launcher_unreachable");
    expect((err as LeanSidecarApiError).status).toBe(503);
  });

  it("falls back to reason='unknown' when the body doesn't match the envelope", async () => {
    const promise = service.startTrustedRun(goodRequest).catch((e) => e);
    const req = httpMock.expectOne((r) => r.url.endsWith("/api/lean-sidecar/trusted-runs"));
    req.flush("server exploded", { status: 500, statusText: "Internal Server Error" });
    const err = await promise;
    expect((err as LeanSidecarApiError).reason).toBe("unknown");
    expect((err as LeanSidecarApiError).status).toBe(500);
  });

  it("URL-encodes the run_id on the inspection endpoints", async () => {
    // A run_id containing characters that would need encoding could
    // never reach this point (the server rejects them), but the
    // service should still encode defensively so a misuse from a
    // future call site doesn't break the URL.
    const promise = service.getNormalized("a b/c");
    const req = httpMock.expectOne((r) => r.url.includes("/runs/a%20b%2Fc/normalized"));
    req.flush({ parser_version: "phase-3a-r1", algorithm_id: "X" });
    const result = await promise;
    expect(result.parser_version).toBe("phase-3a-r1");
  });

  it("returns the log tail as text", async () => {
    const promise = service.getLogTail("ut_run");
    const req = httpMock.expectOne((r) => r.url.endsWith("/api/lean-sidecar/runs/ut_run/log"));
    expect(req.request.responseType).toBe("text");
    req.flush("LEAN ALGORITHMIC TRADING ENGINE v2.5.0.0\n");
    expect(await promise).toContain("LEAN ALGORITHMIC TRADING ENGINE");
  });

  it("Phase 4e: getManifest returns the raw manifest dict", async () => {
    const fakeManifest = {
      run_id: "ut_run_manifest",
      parameters: { symbol: "SPY", starting_cash: "100000" },
      requested_window_ms: { start_ms: 1, end_ms: 2 },
      algorithm_source_sha256: "abc",
    };
    const promise = service.getManifest("ut_run_manifest");
    const req = httpMock.expectOne((r) =>
      r.url.endsWith("/api/lean-sidecar/runs/ut_run_manifest/manifest"),
    );
    req.flush(fakeManifest);
    const result = await promise;
    expect(result.parameters?.symbol).toBe("SPY");
    expect(result.requested_window_ms?.start_ms).toBe(1);
  });

  it("Phase 4e: getManifest maps a 404 to the typed envelope", async () => {
    const promise = service.getManifest("missing").catch((e) => e);
    const req = httpMock.expectOne((r) => r.url.endsWith("/manifest"));
    req.flush(
      { detail: { reason: "manifest_missing", message: "no manifest.json for missing" } },
      { status: 404, statusText: "Not Found" },
    );
    const err = await promise;
    expect((err as LeanSidecarApiError).reason).toBe("manifest_missing");
    expect((err as LeanSidecarApiError).status).toBe(404);
  });

  it("Phase 5a: reconcileRun POSTs to /runs/{id}/reconcile and parses the report", async () => {
    const fakeReport = {
      run_id: "ut_run_reconcile",
      algorithm_id: "MyAlgorithm",
      normalized_parser_version: "phase-3a-r1",
      total_fill_events: 2,
      matched_count: 1,
      divergent_count: 1,
      commission_atol: "0.01",
      total_recorded_fees: "6.00",
      total_expected_ibkr_fees: "2.00",
      divergences: [
        {
          order_event_id: 2,
          order_id: 200,
          symbol: "SPY",
          ms_utc: 1_736_121_600_000,
          fill_quantity: 100,
          fill_price: "580.50",
          recorded_fee: "5.00",
          expected_ibkr_fee: "1.00",
          delta: "4.00",
          category: "commission_drift",
        },
      ],
    };
    const promise = service.reconcileRun("ut_run_reconcile");
    const req = httpMock.expectOne(
      (r) =>
        r.method === "POST" &&
        r.url.endsWith("/api/lean-sidecar/runs/ut_run_reconcile/reconcile"),
    );
    req.flush(fakeReport);
    const result = await promise;
    expect(result.run_id).toBe("ut_run_reconcile");
    expect(result.divergent_count).toBe(1);
    expect(result.divergences[0].category).toBe("commission_drift");
    // Money strings preserved exactly (no float parsing).
    expect(result.total_recorded_fees).toBe("6.00");
  });

  it("Phase 5a: reconcileRun maps a 404 envelope to a typed error", async () => {
    const promise = service.reconcileRun("ut_run_missing").catch((e) => e);
    const req = httpMock.expectOne((r) => r.url.endsWith("/reconcile"));
    req.flush(
      {
        detail: {
          reason: "normalized_missing",
          message: "cannot reconcile ut_run_missing: normalized result not present",
        },
      },
      { status: 404, statusText: "Not Found" },
    );
    const err = await promise;
    expect((err as LeanSidecarApiError).reason).toBe("normalized_missing");
    expect((err as LeanSidecarApiError).status).toBe(404);
  });
});
