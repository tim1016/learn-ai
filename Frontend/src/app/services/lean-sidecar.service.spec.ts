import { provideZonelessChangeDetection } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { provideHttpClient } from "@angular/common/http";
import {
  HttpTestingController,
  provideHttpClientTesting,
} from "@angular/common/http/testing";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  LeanSidecarApiError,
  LeanSidecarService,
} from "./lean-sidecar.service";
import type {
  TrustedRunRequest,
  TrustedRunResponse,
} from "./lean-sidecar.types";

/**
 * Tests that the launcher's ``{detail: {reason, message}}`` rejection
 * envelope round-trips into a typed ``LeanSidecarApiError`` so the
 * component can branch on ``reason`` without parsing free text. This
 * is the contract the data-plane router promises (see PR #249).
 *
 * PR B.5 (2026-05-19) — surface narrowed alongside the ``/lean-lab``
 * retirement. The remaining test set covers ``startTrustedRun`` only;
 * the inspection / reconciliation / manifest helpers' tests went with
 * their methods.
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
        benchmark_unavailable: [],
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
    const req = httpMock.expectOne(
      (r) =>
        r.method === "POST" && r.url.endsWith("/api/lean-sidecar/trusted-runs"),
    );
    expect(req.request.body).toEqual(goodRequest);
    req.flush(fakeResponse);

    const result = await promise;
    expect(result.is_clean).toBe(true);
    expect(result.normalized_parser_version).toBe("phase-3a-r1");
  });

  it("turns the launcher 400 envelope into a typed LeanSidecarApiError", async () => {
    const promise = service.startTrustedRun(goodRequest).catch((e) => e);
    const req = httpMock.expectOne((r) =>
      r.url.endsWith("/api/lean-sidecar/trusted-runs"),
    );
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
    const req = httpMock.expectOne((r) =>
      r.url.endsWith("/api/lean-sidecar/trusted-runs"),
    );
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
    const req = httpMock.expectOne((r) =>
      r.url.endsWith("/api/lean-sidecar/trusted-runs"),
    );
    req.flush("server exploded", {
      status: 500,
      statusText: "Internal Server Error",
    });
    const err = await promise;
    expect((err as LeanSidecarApiError).reason).toBe("unknown");
    expect((err as LeanSidecarApiError).status).toBe(500);
  });

  it("nextTradingDayOpen GETs /calendar/next-trading-day-open with the date param and returns the body", async () => {
    const promise = service.nextTradingDayOpen("2025-01-17");
    const req = httpMock.expectOne(
      (r) =>
        r.method === "GET" &&
        r.url.endsWith("/api/lean-sidecar/calendar/next-trading-day-open"),
    );
    expect(req.request.params.get("date")).toBe("2025-01-17");
    req.flush({
      next_trading_date: "2025-01-21",
      session_open_ms_utc: 1737466200000,
    });
    const result = await promise;
    expect(result.next_trading_date).toBe("2025-01-21");
    expect(result.session_open_ms_utc).toBe(1737466200000);
  });

  it("nextTradingDayOpen translates a launcher error envelope to LeanSidecarApiError", async () => {
    const promise = service.nextTradingDayOpen("9999-99-99").catch((e) => e);
    const req = httpMock.expectOne((r) =>
      r.url.endsWith("/api/lean-sidecar/calendar/next-trading-day-open"),
    );
    req.flush(
      { detail: { reason: "no_session_in_range", message: "no NYSE session within 14 days after 9999-99-99" } },
      { status: 422, statusText: "Unprocessable Entity" },
    );
    const err = await promise;
    expect(err).toBeInstanceOf(LeanSidecarApiError);
    expect((err as LeanSidecarApiError).reason).toBe("no_session_in_range");
    expect((err as LeanSidecarApiError).status).toBe(422);
  });
});
