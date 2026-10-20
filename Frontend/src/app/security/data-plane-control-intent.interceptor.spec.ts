import { TestBed } from "@angular/core/testing";
import { provideHttpClient, withInterceptors } from "@angular/common/http";
import { HttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  DATA_PLANE_CONTROL_INTENT_HEADER,
  DATA_PLANE_CONTROL_INTENT_VALUE,
  DATA_PLANE_CONTROL_PREFIXES,
  DATA_PLANE_CONTROL_PROTECTED_READ_PREFIXES,
  dataPlaneControlIntentInterceptor,
} from "./data-plane-control-intent.interceptor";

describe("dataPlaneControlIntentInterceptor", () => {
  let http: HttpClient;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(withInterceptors([dataPlaneControlIntentInterceptor])),
        provideHttpClientTesting(),
      ],
    });
    http = TestBed.inject(HttpClient);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    httpMock.verify();
  });

  for (const prefix of DATA_PLANE_CONTROL_PREFIXES) {
    it(`marks unsafe ${prefix} control requests with the browser intent header`, () => {
      const url = `${prefix}/__probe`;
      http.post(url, {}).subscribe();

      const req = httpMock.expectOne(url);
      expect(req.request.headers.get(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(
        DATA_PLANE_CONTROL_INTENT_VALUE,
      );
      expect(req.request.headers.has("Cache-Control")).toBe(false);
      req.flush({});
    });
  }

  for (const prefix of DATA_PLANE_CONTROL_PROTECTED_READ_PREFIXES) {
    it(`marks protected ${prefix} reads with the browser intent header`, () => {
      const url = `${prefix}/__probe`;
      http.get(url).subscribe();

      const req = httpMock.expectOne(url);
      expect(req.request.headers.get(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(
        DATA_PLANE_CONTROL_INTENT_VALUE,
      );
      expect(req.request.headers.get("Cache-Control")).toBe("no-cache, no-store");
      req.flush({});
    });
  }

  it.each([
    ["account transaction reads", "/api/accounts/DU1234567/transactions"],
    // /api/broker-clerks sits outside the /api/brokers prefix and carries
    // the always-on control-secret guard server-side; without a manifest
    // entry the proxy never attaches the secret and the lane directory
    // 403s in every secret-configured deployment.
    ["fleet directory reads", "/api/broker-clerks"],
  ])("marks %s that the data plane always protects (%s)", (_name, url) => {
    http.get(url).subscribe();

    const req = httpMock.expectOne(url);
    expect(req.request.headers.get(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(
      DATA_PLANE_CONTROL_INTENT_VALUE,
    );
    req.flush({});
  });

  it("does not mark unprotected control reads", () => {
    http.get("/api/broker/health").subscribe();

    const req = httpMock.expectOne("/api/broker/health");
    expect(req.request.headers.has(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(false);
    expect(req.request.headers.has("Cache-Control")).toBe(false);
    req.flush({});
  });

  it("revalidates account-mode reads even when a caller allows cached responses", () => {
    const url = "/api/brokers/alpaca/clerks/clrk_live/live-verdict";
    http.get(url, { headers: { "Cache-Control": "max-age=3600" } }).subscribe();

    const req = httpMock.expectOne(url);
    expect(req.request.headers.get("Cache-Control")).toBe("no-cache, no-store");
    req.flush({ final_verdict: "live" });
  });

  it("requires fresh metadata for protected HEAD reads", () => {
    const url = "/api/broker-clerks";
    http.head(url).subscribe();

    const req = httpMock.expectOne(url);
    expect(req.request.headers.get("Cache-Control")).toBe("no-cache, no-store");
    req.flush(null);
  });

  it.each([
    ["non-control mutations", "/api/research/strategy-runs"],
    ["similar non-control prefixes", "/api/brokerage/connect"],
  ])("does not mark %s (POST %s)", (_name, url) => {
    http.post(url, {}).subscribe();

    const req = httpMock.expectOne(url);
    expect(req.request.headers.has(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(false);
    req.flush({});
  });
});
