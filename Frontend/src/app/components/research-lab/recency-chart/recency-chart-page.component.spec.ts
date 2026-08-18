import { ComponentFixture, TestBed } from "@angular/core/testing";
import { provideZonelessChangeDetection, signal } from "@angular/core";
import { HttpClient } from "@angular/common/http";
import { Apollo } from "apollo-angular";
import { MessageService } from "primeng/api";
import { from, of, throwError } from "rxjs";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fireEvent, screen } from "@testing-library/angular";

import { RecencyChartPageComponent } from "./recency-chart-page.component";
import { JobsService } from "../../../services/jobs.service";
import {
  RECENCY_HERO_QUERY,
  RECENCY_TRADES_QUERY,
  SOFT_DELETE_RECENCY_RUN_MUTATION,
  type RecencyHeroQueryResultItem,
  type RecencyTradeQueryResultItem,
} from "../../../graphql/recency-chart.query";

function makeTrade(overrides: Partial<RecencyTradeQueryResultItem> = {}): RecencyTradeQueryResultItem {
  // Default entry/exit are "now"-relative, not epoch-relative: the page
  // computes a real Date.now()-anchored display window (design spec
  // D18-D19), so a trade near the Unix epoch would always be virtualized
  // out. Tests that specifically exercise window-cap behavior override
  // entryMs/exitMs explicitly.
  const recentEntry = Date.now() - 60_000;
  return {
    symbol: "SPY",
    strategyKey: "ema_crossover_2_bps",
    paramsHash: "hash1",
    paramsJson: "{}",
    fingerprint: "fp1",
    entryMs: recentEntry,
    exitMs: recentEntry + 1_000,
    pnlPts: 2,
    pnlPct: 0.02,
    quantity: 10,
    pnl: 20,
    holdingSessions: 1,
    sharpe: 1.0,
    studyId: null,
    recencyRunId: 1,
    isSyntheticExit: false,
    signalReason: "",
    memberships: [{ recencyRunId: 1, studyId: null, createdAtMs: recentEntry }],
    ...overrides,
  };
}

function makeHero(overrides: Partial<RecencyHeroQueryResultItem> = {}): RecencyHeroQueryResultItem {
  return {
    symbol: "SPY",
    strategyKey: "ema_crossover_2_bps",
    paramsHash: "hash1",
    totalPnl: 20,
    recencyRunId: 1,
    ...overrides,
  };
}

let watchQueryMock: ReturnType<typeof vi.fn>;
let mutateMock: ReturnType<typeof vi.fn>;
let messageServiceMock: { add: ReturnType<typeof vi.fn> };

async function renderPage(
  trades: RecencyTradeQueryResultItem[] = [makeTrade()],
  heroes: RecencyHeroQueryResultItem[] | null = null,
  mutateImpl: ReturnType<typeof vi.fn> = vi.fn(() =>
    of({ data: { softDeleteRecencyRun: { recencyRunId: 1 } } }),
  ),
): Promise<ComponentFixture<RecencyChartPageComponent>> {
  // heroes defaults to "one hero per distinct (symbol, strategyKey, paramsHash)
  // seen in trades" so tests that don't care about fold/unfold don't need to
  // hand-author a matching hero list.
  const effectiveHeroes =
    heroes ??
    Array.from(new Map(trades.map((t) => [`${t.symbol}::${t.strategyKey}::${t.paramsHash}`, t])).values()).map(
      (t) => makeHero({ symbol: t.symbol, strategyKey: t.strategyKey, paramsHash: t.paramsHash, totalPnl: t.pnl }),
    );

  watchQueryMock = vi.fn((options: { query: unknown }) => {
    if (options.query === RECENCY_HERO_QUERY) {
      return { valueChanges: from([{ data: { recencyHero: effectiveHeroes }, loading: false }]), stopPolling: vi.fn() };
    }
    return { valueChanges: from([{ data: { recencyTrades: trades }, loading: false }]), stopPolling: vi.fn() };
  });

  mutateMock = mutateImpl;
  messageServiceMock = { add: vi.fn() };

  await TestBed.configureTestingModule({
    imports: [RecencyChartPageComponent],
    providers: [
      provideZonelessChangeDetection(),
      { provide: Apollo, useValue: { watchQuery: watchQueryMock, mutate: mutateMock } },
      { provide: HttpClient, useValue: { get: () => of([]) } },
      { provide: JobsService, useValue: { startJob: vi.fn(async () => "job-1") } },
      { provide: MessageService, useValue: messageServiceMock },
    ],
  }).compileComponents();

  const fixture = TestBed.createComponent(RecencyChartPageComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.restoreAllMocks();
});

describe("RecencyChartPageComponent", () => {
  it("renders a swimlane bar for each trade the query returns", async () => {
    await renderPage([makeTrade({ fingerprint: "a" }), makeTrade({ fingerprint: "b", symbol: "AAPL" })]);

    expect(screen.getByTestId("lane-SPY")).not.toBeNull();
    expect(screen.getByTestId("lane-AAPL")).not.toBeNull();
  });

  it("queries with a numeric fromMs strictly before toMs", async () => {
    await renderPage();

    const tradesCall = watchQueryMock.mock.calls.find(
      (call) => call[0].query === RECENCY_TRADES_QUERY,
    ) as [{ variables: { fromMs: number; toMs: number } }];
    expect(tradesCall[0].variables.fromMs).toBeLessThan(tradesCall[0].variables.toMs);

    const heroCall = watchQueryMock.mock.calls.find(
      (call) => call[0].query === RECENCY_HERO_QUERY,
    ) as [{ variables: { fromMs: number; toMs: number } }];
    expect(heroCall[0].variables.fromMs).toBeLessThan(heroCall[0].variables.toMs);
  });

  it("fetches the full accumulation range, not just the ~6-month all-symbols display cap", async () => {
    // Single-symbol mode is supposed to unlock the launch's full history
    // (up to the 24-month accumulation cap) — the fetch itself must not
    // be truncated to 6 months, or narrowing to one symbol would just
    // re-filter an already-too-small result set.
    await renderPage();

    const tradesCall = watchQueryMock.mock.calls.find(
      (call) => call[0].query === RECENCY_TRADES_QUERY,
    ) as [{ variables: { fromMs: number; toMs: number } }];
    const spanMs = tradesCall[0].variables.toMs - tradesCall[0].variables.fromMs;
    const thirteenMonthsMs = 1000 * 60 * 60 * 24 * 30 * 13;
    expect(spanMs).toBeGreaterThan(thirteenMonthsMs);
  });

  it("shows an empty-state message when no trades are returned", async () => {
    await renderPage([]);

    expect(screen.getByText(/no timeline data/i)).not.toBeNull();
  });

  it("toggling a symbol off removes its lane from the swimlane", async () => {
    const fixture = await renderPage([makeTrade({ fingerprint: "a" }), makeTrade({ fingerprint: "b", symbol: "AAPL" })]);

    expect(screen.getByTestId("lane-AAPL")).not.toBeNull();
    fireEvent.click(screen.getByRole("checkbox", { name: /^AAPL$/i }));
    await fixture.whenStable();

    expect(screen.queryByTestId("lane-AAPL")).toBeNull();
    expect(screen.getByTestId("lane-SPY")).not.toBeNull();
  });

  it("narrowing to a single visible symbol unlocks a trade older than the all-symbols window cap", async () => {
    const now = Date.now();
    const recent = now - 60_000; // within the ~1-trading-week all-symbols cap
    const old = now - 30 * 24 * 60 * 60_000; // 30 days back — well beyond the cap

    const fixture = await renderPage([
      makeTrade({ fingerprint: "recent", symbol: "SPY", entryMs: recent, exitMs: recent + 1_000 }),
      makeTrade({ fingerprint: "stale", symbol: "OLD", entryMs: old, exitMs: old + 1_000 }),
    ]);

    // All-symbols mode: OLD's stale trade sits outside the ~1-week cap.
    expect(screen.queryByTestId("lane-OLD")).toBeNull();

    fireEvent.click(screen.getByRole("checkbox", { name: /^SPY$/i }));
    await fixture.whenStable();

    // Single-symbol mode (only OLD left visible): the long window unlocks it.
    expect(screen.getByTestId("lane-OLD")).not.toBeNull();
  });

  it("shows only the hero combo by default and reveals the rest on expand", async () => {
    const fixture = await renderPage(
      [
        makeTrade({ fingerprint: "hero", paramsHash: "hero-hash", pnl: 50 }),
        makeTrade({ fingerprint: "other", paramsHash: "other-hash", pnl: 5 }),
      ],
      [makeHero({ paramsHash: "hero-hash", totalPnl: 50 })],
    );

    expect(screen.getByRole("button", { name: /ema_crossover_2_bps, \+50\.00/i })).not.toBeNull();
    expect(screen.queryByRole("button", { name: /ema_crossover_2_bps, \+5\.00/i })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: /combos/i }));
    await fixture.whenStable();

    expect(screen.getByRole("button", { name: /ema_crossover_2_bps, \+5\.00/i })).not.toBeNull();
  });

  it("clicking a bar pins its details into the focus panel", async () => {
    const fixture = await renderPage([makeTrade({ fingerprint: "a", pnl: 30 })]);

    fireEvent.click(screen.getByRole("button", { name: /\+30\.00 PnL/i }));
    await fixture.whenStable();

    expect(screen.getByText("+30.00")).not.toBeNull();
  });

  it("deleting a run refetches membership-aware data instead of optimistically hiding the trade", async () => {
    const fixture = await renderPage([
      makeTrade({ fingerprint: "a", symbol: "SPY", pnl: 30, recencyRunId: 7 }),
      makeTrade({ fingerprint: "b", symbol: "AAPL", pnl: 10, recencyRunId: 8 }),
    ]);

    fireEvent.click(screen.getByRole("button", { name: /\+30\.00 PnL/i }));
    await fixture.whenStable();

    fireEvent.click(screen.getByRole("button", { name: /delete run/i }));
    await fixture.whenStable();

    expect(mutateMock).toHaveBeenCalledWith(
      expect.objectContaining({
        mutation: SOFT_DELETE_RECENCY_RUN_MUTATION,
        variables: { runId: 7 },
      }),
    );
    // The static test response still includes SPY, modeling a surviving
    // membership on the same canonical trade. The client must not hide it
    // merely because the deleted run was its previous representative.
    expect(screen.getByTestId("lane-SPY")).not.toBeNull();
    expect(screen.getByTestId("lane-AAPL")).not.toBeNull();
    expect(screen.getByText(/click a bar/i)).not.toBeNull();
  });

  it("shows an error toast and keeps the trade visible when the delete mutation fails", async () => {
    const failingMutate = vi.fn(() => throwError(() => new Error("boom")));
    const fixture = await renderPage(
      [makeTrade({ fingerprint: "a", symbol: "SPY", pnl: 30, recencyRunId: 7 })],
      null,
      failingMutate,
    );

    fireEvent.click(screen.getByRole("button", { name: /\+30\.00 PnL/i }));
    await fixture.whenStable();

    fireEvent.click(screen.getByRole("button", { name: /delete run/i }));
    await fixture.whenStable();

    expect(messageServiceMock.add).toHaveBeenCalledWith(expect.objectContaining({ severity: "error" }));
    expect(screen.getByTestId("lane-SPY")).not.toBeNull();
  });

  it("reloads the trades and hero projections once a launched job completes", async () => {
    // startJob only resolves once Python accepts the job (202) — long
    // before persistence finishes. Without a reload-on-complete, a fresh
    // launch's trades stay invisible until a manual page reload.
    const jobState = signal<{ status: string } | undefined>(undefined);
    watchQueryMock = vi.fn((options: { query: unknown }) => {
      if (options.query === RECENCY_HERO_QUERY) {
        return { valueChanges: from([{ data: { recencyHero: [] }, loading: false }]), stopPolling: vi.fn() };
      }
      return { valueChanges: from([{ data: { recencyTrades: [] }, loading: false }]), stopPolling: vi.fn() };
    });
    messageServiceMock = { add: vi.fn() };

    const strategies = [
      {
        name: "ema_crossover_2_bps",
        display_name: "EMA Crossover (2 bps)",
        description: "",
        params_schema: { properties: { symbol: { type: "string", default: "SPY" } } },
        supported_resolutions: ["minute"],
        strategy_bars: { timespan: "minute", multiplier: 15 },
        recency_supported: true,
      },
    ];

    await TestBed.configureTestingModule({
      imports: [RecencyChartPageComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: Apollo, useValue: { watchQuery: watchQueryMock, mutate: vi.fn() } },
        { provide: HttpClient, useValue: { get: () => of(strategies) } },
        {
          provide: JobsService,
          useValue: {
            startJob: vi.fn(async () => "job-1"),
            job: (id: string) => (id === "job-1" ? jobState() : undefined),
          },
        },
        { provide: MessageService, useValue: messageServiceMock },
      ],
    }).compileComponents();

    const fixture = TestBed.createComponent(RecencyChartPageComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    fixture.detectChanges();

    // The only catalog strategy is preselected, so this test must NOT toggle the
    // checkbox: doing so deselected it and launched a zero-strategy job, which
    // the launch guard now refuses.
    expect(
      (screen.getByRole("checkbox", { name: /ema crossover \(2 bps\)/i }) as HTMLInputElement).checked,
    ).toBe(true);
    fireEvent.click(screen.getByRole("button", { name: /launch/i }));
    await fixture.whenStable();

    const callsBeforeCompletion = watchQueryMock.mock.calls.length;

    jobState.set({ status: "completed" });
    await fixture.whenStable();

    expect(watchQueryMock.mock.calls.length).toBeGreaterThan(callsBeforeCompletion);
  });
});
