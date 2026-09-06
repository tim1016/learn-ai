import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { TestBed } from "@angular/core/testing";
import { firstValueFrom } from "rxjs";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { environment } from "../../environments/environment";
import { makeRun } from "../components/strategy-lab/testing/run-fixtures";
import { BacktestRunsService, HISTORY_PAGE_SIZE } from "./backtest-runs.service";

const BASE = `${environment.pythonServiceUrl}/api/research/backtest-runs`;

describe("BacktestRunsService", () => {
  let service: BacktestRunsService;
  let http: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({ providers: [provideHttpClient(), provideHttpClientTesting()] });
    service = TestBed.inject(BacktestRunsService);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => http.verify());

  it("lists the fixed history page and omits the engine filter when every engine is wanted", async () => {
    const pending = firstValueFrom(service.list(null));

    const request = http.expectOne((r) => r.url === BASE);
    expect(request.request.method).toBe("GET");
    expect(request.request.params.get("limit")).toBe(String(HISTORY_PAGE_SIZE));
    expect(request.request.params.has("engine")).toBe(false);
    request.flush([]);

    expect(await pending).toEqual([]);
  });

  it("passes the engine filter through as the GraphQL-era enum value", async () => {
    const pending = firstValueFrom(service.list("LEAN", 10));

    const request = http.expectOne((r) => r.url === BASE);
    expect(request.request.params.get("engine")).toBe("LEAN");
    expect(request.request.params.get("limit")).toBe("10");
    request.flush([]);
    await pending;
  });

  it("returns the run report as served", async () => {
    const run = makeRun({ id: 7 });
    const pending = firstValueFrom(service.get(7));

    http.expectOne(`${BASE}/7`).flush(run);

    expect(await pending).toEqual(run);
  });

  it("answers a run the server does not have with null, not an error", async () => {
    const pending = firstValueFrom(service.get(404));

    http.expectOne(`${BASE}/404`).flush({ detail: { code: "BACKTEST_RUN_NOT_FOUND" } }, { status: 404, statusText: "Not Found" });

    expect(await pending).toBeNull();
  });

  it("lets any other failure propagate", async () => {
    const pending = firstValueFrom(service.get(9));

    http.expectOne(`${BASE}/9`).flush("boom", { status: 503, statusText: "Unavailable" });

    await expect(pending).rejects.toMatchObject({ status: 503 });
  });

  it("patches the notes and returns the persisted value", async () => {
    const pending = firstValueFrom(service.updateNotes(7, "keep"));

    const request = http.expectOne(`${BASE}/7/notes`);
    expect(request.request.method).toBe("PATCH");
    expect(request.request.body).toEqual({ notes: "keep" });
    request.flush({ id: 7, notes: "keep" });

    expect(await pending).toEqual({ id: 7, notes: "keep" });
  });
});
