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

/**
 * Tests that the launcher's ``{detail: {reason, message}}`` rejection
 * envelope round-trips into a typed ``LeanSidecarApiError`` so the
 * component can branch on ``reason`` without parsing free text. This
 * is the contract the data-plane router promises (see PR #249).
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

  it("diagnose GETs the launcher health report", async () => {
    const promise = service.diagnose();
    const req = httpMock.expectOne(
      (r) =>
        r.method === "GET" && r.url.endsWith("/api/lean-sidecar/diagnose"),
    );
    req.flush({
      overall_status: "pass",
      fetched_at_ms: 1_783_875_135_460,
      checks: [
        {
          name: "launcher_healthz",
          label: "GET launcher /healthz",
          status: "pass",
          detail: "200 OK",
          fix: null,
        },
      ],
    });

    const result = await promise;
    expect(result.overall_status).toBe("pass");
    expect(result.checks[0].name).toBe("launcher_healthz");
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
