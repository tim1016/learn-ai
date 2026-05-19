import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { Router } from "@angular/router";
import { Apollo } from "apollo-angular";
import { of } from "rxjs";
import { describe, expect, it, vi } from "vitest";

import { LeanLabRunHistoryStripComponent } from "./lean-lab-run-history-strip.component";
import { BacktestRunNode } from "../../../graphql/backtest-runs.query";

const FAKE_NODES: BacktestRunNode[] = [
  {
    id: "10",
    source: "lean-sidecar",
    strategyName: "ema_crossover",
    leanRunId: "ui_run_abc",
    parameters: '{"symbol":"SPY","starting_cash":100000}',
    startDate: "2025-01-06",
    endDate: "2025-01-10",
    executedAt: "2026-05-19T02:49:00Z",
    totalTrades: 1,
    totalPnL: 9.0,
    trades: [{ isSyntheticExit: false }],
  },
  {
    id: "11",
    source: "lean-sidecar",
    strategyName: "trusted_default",
    leanRunId: "ui_run_xyz",
    parameters: '{"symbol":"SPY","starting_cash":100000}',
    startDate: "2025-01-06",
    endDate: "2025-01-06",
    executedAt: "2026-05-19T02:50:00Z",
    totalTrades: 1,
    totalPnL: 5.0,
    trades: [{ isSyntheticExit: true }],
  },
];

function makeApollo(nodes: BacktestRunNode[] = FAKE_NODES) {
  const valueChanges$ = of({
    data: {
      backtestRuns: {
        pageInfo: { hasNextPage: false, endCursor: null },
        nodes,
      },
    },
  });
  return { watchQuery: vi.fn().mockReturnValue({ valueChanges: valueChanges$ }) };
}

async function setup(
  apolloStub = makeApollo(),
): Promise<ComponentFixture<LeanLabRunHistoryStripComponent>> {
  await TestBed.configureTestingModule({
    imports: [LeanLabRunHistoryStripComponent],
    providers: [
      provideZonelessChangeDetection(),
      { provide: Apollo, useValue: apolloStub },
      { provide: Router, useValue: { navigate: vi.fn() } },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(LeanLabRunHistoryStripComponent);
  fixture.detectChanges();
  return fixture;
}

describe("LeanLabRunHistoryStripComponent", () => {
  it("renders the run-history table hidden by default", async () => {
    const fixture = await setup();
    const panel = fixture.nativeElement.querySelector(
      "#lean-lab-run-history-panel",
    ) as HTMLElement;
    expect(panel.hasAttribute("hidden")).toBe(true);
  });

  it("toggle button shows 'Show history' with the run count when collapsed", async () => {
    const fixture = await setup();
    const button = fixture.nativeElement.querySelector(
      ".strip__toggle",
    ) as HTMLButtonElement;
    expect(button.textContent ?? "").toContain("Show history");
    expect(button.textContent ?? "").toContain("(2)");
    expect(button.getAttribute("aria-expanded")).toBe("false");
    expect(button.getAttribute("aria-controls")).toBe(
      "lean-lab-run-history-panel",
    );
  });

  it("renders the most recent run summary line", async () => {
    const fixture = await setup();
    const meta = fixture.nativeElement.querySelector(
      ".strip__meta",
    ) as HTMLElement;
    const text = meta.textContent ?? "";
    expect(text).toContain("ui_run_abc");
    expect(text).toContain("ema_crossover");
    expect(text).toContain("2025-01-06");
    expect(text).toContain("2025-01-10");
  });

  it("expands when the toggle is clicked and reveals the inner table", async () => {
    const fixture = await setup();
    const button = fixture.nativeElement.querySelector(
      ".strip__toggle",
    ) as HTMLButtonElement;

    button.click();
    fixture.detectChanges();

    const panel = fixture.nativeElement.querySelector(
      "#lean-lab-run-history-panel",
    ) as HTMLElement;
    expect(panel.hasAttribute("hidden")).toBe(false);
    expect(button.getAttribute("aria-expanded")).toBe("true");
    expect(button.textContent ?? "").toContain("Hide history");

    // The inner app-lean-lab-run-history renders a table when rows exist.
    const table = fixture.nativeElement.querySelector("table");
    expect(table).not.toBeNull();
  });

  it("re-emits runSelected from the inner component", async () => {
    const emitted: string[] = [];
    const fixture = await setup();
    fixture.componentInstance.runSelected.subscribe((id) => emitted.push(id));

    fixture.componentInstance.onRunSelected("ui_run_abc");

    expect(emitted).toEqual(["ui_run_abc"]);
  });

  it("renders the empty meta when no runs exist", async () => {
    const fixture = await setup(makeApollo([]));
    const meta = fixture.nativeElement.querySelector(
      ".strip__meta",
    ) as HTMLElement;
    expect(meta.textContent ?? "").toContain("No runs yet");
    expect(meta.classList.contains("strip__meta--empty")).toBe(true);
  });
});
