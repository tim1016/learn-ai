import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { TestBed } from "@angular/core/testing";
import { afterEach, describe, expect, it } from "vitest";

import { LeanSourceService } from "./lean-source.service";

function setup() {
  TestBed.configureTestingModule({
    providers: [provideHttpClient(), provideHttpClientTesting(), LeanSourceService],
  });
  return { service: TestBed.inject(LeanSourceService), http: TestBed.inject(HttpTestingController) };
}

afterEach(() => TestBed.resetTestingModule());

describe("LeanSourceService", () => {
  it("returns the registered twin when one exists", async () => {
    const { service, http } = setup();
    const pending = service.getStrategySource("rsi_mean_reversion");
    http.expectOne((request) => request.url.endsWith("/rsi_mean_reversion/lean-source")).flush({
      strategy_name: "rsi_mean_reversion", template: "rsi_mean_reversion", language: "python",
      source: "class A(QCAlgorithm): pass", source_sha256: "a".repeat(64),
    });

    expect(await pending).toEqual(expect.objectContaining({ kind: "available" }));
  });

  it("reports an unregistered twin as a fact about the strategy, not a failure", async () => {
    const { service, http } = setup();
    const pending = service.getStrategySource("sma_crossover");
    http.expectOne((request) => request.url.endsWith("/sma_crossover/lean-source")).flush(
      { detail: "Strategy 'sma_crossover' has no registered LEAN validation source" },
      { status: 404, statusText: "Not Found" },
    );

    expect(await pending).toEqual({
      kind: "unregistered",
      detail: "Strategy 'sma_crossover' has no registered LEAN validation source",
    });
  });

  it("keeps a transport failure distinguishable from an unregistered twin", async () => {
    const { service, http } = setup();
    const pending = service.getStrategySource("rsi_mean_reversion");
    http.expectOne((request) => request.url.endsWith("/rsi_mean_reversion/lean-source"))
      .flush("boom", { status: 500, statusText: "Server Error" });

    expect((await pending).kind).toBe("unavailable");
  });
});
