import { provideHttpClient } from "@angular/common/http";
import { HttpTestingController, provideHttpClientTesting } from "@angular/common/http/testing";
import { signal } from "@angular/core";
import { TestBed } from "@angular/core/testing";
import { ActivatedRoute, convertToParamMap } from "@angular/router";
import { BehaviorSubject } from "rxjs";
import { afterEach, describe, expect, it } from "vitest";

import { provideFakeDateWorld } from "../../shared/date-picker/testing/fake-trading-calendar";
import { fakeTickerCatalog, provideFakeTickerCatalog } from "../../shared/ticker-catalog/testing/fake-ticker-catalog";
import { STRATEGY_LAB_PINNED_STRATEGY, StrategyLabConfigStore } from "./strategy-lab-config.store";
import type { StrategyInfo } from "./strategy-lab.models";

const EMA = "ema_crossover_signal";
const RSI = "rsi_mean_reversion";

function strategy(name: string, overrides: Partial<StrategyInfo> = {}): StrategyInfo {
  return {
    name,
    display_name: name === EMA ? "EMA crossover" : "RSI mean reversion",
    description: "",
    params_schema: { properties: { symbol: { type: "string", default: "SPY" }, window: { type: "integer", default: 14 } } },
    supported_resolutions: ["minute"],
    strategy_bars: { timespan: "minute", multiplier: 15, parameter: null },
    lean_twin: name,
    ...overrides,
  };
}

function configure(pinned: string | null, query: Record<string, string> = {}) {
  const queryParamMap = new BehaviorSubject(convertToParamMap(query));
  TestBed.configureTestingModule({
    providers: [
      provideHttpClient(),
      provideHttpClientTesting(),
      provideFakeTickerCatalog(fakeTickerCatalog([{ symbol: "SPY", name: "SPDR S&P 500 ETF Trust", firstHeld: "2024-01-02", lastHeld: "2026-09-18" }])),
      ...provideFakeDateWorld(),
      StrategyLabConfigStore,
      { provide: ActivatedRoute, useValue: { queryParamMap } },
      { provide: STRATEGY_LAB_PINNED_STRATEGY, useValue: signal(pinned).asReadonly() },
    ],
  });
  return { store: TestBed.inject(StrategyLabConfigStore), http: TestBed.inject(HttpTestingController) };
}

afterEach(() => {
  TestBed.resetTestingModule();
});

describe("a workbench pinned to one strategy", () => {
  it("offers and selects only that strategy, though the catalog lists others first", async () => {
    const { store, http } = configure(RSI);
    const loaded = store.loadStrategies();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush([strategy(EMA), strategy(RSI)]);
    await loaded;
    TestBed.tick();

    expect(store.selectableStrategies().map((candidate) => candidate.name)).toEqual([RSI]);
    expect(store.selectedStrategyName()).toBe(RSI);
  });

  it("takes its strategy from the pin, not from a ?strategy= in the link", async () => {
    const { store, http } = configure(RSI, { strategy: EMA, symbol: "QQQ" });
    const loaded = store.loadStrategies();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush([strategy(EMA), strategy(RSI)]);
    await loaded;
    TestBed.tick();

    expect(store.selectedStrategyName()).toBe(RSI);
    expect(store.range().symbol).toBe("QQQ");
  });

  it("reads the strategy list once however many times a tool asks for it", async () => {
    const { store, http } = configure(EMA);

    const first = store.ensureStrategiesLoaded();
    const second = store.ensureStrategiesLoaded();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush([strategy(EMA)]);
    await Promise.all([first, second]);

    expect(second).toBe(first);
    http.verify();
  });

  it("greys the LEAN engines, with why, for a strategy that has no LEAN twin", async () => {
    const { store, http } = configure(EMA);
    const loaded = store.loadStrategies();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush([strategy(EMA, { lean_twin: null })]);
    await loaded;

    expect(Object.keys(store.engineRestrictions()).sort()).toEqual(["both", "lean"]);
    expect(store.engineRestrictions().lean).toContain("no aligned LEAN validation template");
  });

  it("keeps the Python engine, and says why, when the link asks for an engine the strategy cannot run", async () => {
    const { store, http } = configure(EMA, { engine: "both" });
    const loaded = store.loadStrategies();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush([strategy(EMA, { lean_twin: null })]);
    await loaded;
    TestBed.tick();

    expect(store.engine()).toBe("python");
    expect(store.configurationWarning()).toContain("The link's both engine was not applied");
  });

  it("is open to every strategy when nothing pins it", async () => {
    const { store, http } = configure(null);
    const loaded = store.loadStrategies();
    http.expectOne((request) => request.url.endsWith("/api/engine/strategies")).flush([strategy(EMA), strategy(RSI)]);
    await loaded;

    expect(store.selectableStrategies().map((candidate) => candidate.name)).toEqual([EMA, RSI]);
    expect(store.engineRestrictions()).toEqual({});
  });
});
