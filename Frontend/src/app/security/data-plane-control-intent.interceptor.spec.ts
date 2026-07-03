import { TestBed } from "@angular/core/testing";
import { provideHttpClient, withInterceptors } from "@angular/common/http";
import { HttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import {
  DATA_PLANE_CONTROL_INTENT_HEADER,
  DATA_PLANE_CONTROL_INTENT_VALUE,
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

  it("marks unsafe broker control requests with the browser intent header", () => {
    http.post("/api/broker/connect", {}).subscribe();

    const req = httpMock.expectOne("/api/broker/connect");
    expect(req.request.headers.get(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(DATA_PLANE_CONTROL_INTENT_VALUE);
    req.flush({});
  });

  it("marks unsafe live control requests with the browser intent header", () => {
    http.delete("/api/live-instances/orders/42").subscribe();

    const req = httpMock.expectOne("/api/live-instances/orders/42");
    expect(req.request.headers.get(DATA_PLANE_CONTROL_INTENT_HEADER)).toBe(DATA_PLANE_CONTROL_INTENT_VALUE);
    req.flush({});
  });

  it("does not mark safe control reads", () => {
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
});
