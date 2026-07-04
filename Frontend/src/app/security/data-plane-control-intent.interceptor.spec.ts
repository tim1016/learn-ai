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
      req.flush({});
    });
  }

  it("does not mark unprotected control reads", () => {
    http.get("/api/broker/health").subscribe();

    const req = httpMock.expectOne("/api/broker/health");
    expect(req.request.headers.has(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(false);
    req.flush({});
  });

  it("does not mark non-control mutations", () => {
    http.post("/api/research/strategy-runs", {}).subscribe();

    const req = httpMock.expectOne("/api/research/strategy-runs");
    expect(req.request.headers.has(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(false);
    req.flush({});
  });

  it("does not mark similar non-control prefixes", () => {
    http.post("/api/brokerage/connect", {}).subscribe();

    const req = httpMock.expectOne("/api/brokerage/connect");
    expect(req.request.headers.has(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(false);
    req.flush({});
  });
});
