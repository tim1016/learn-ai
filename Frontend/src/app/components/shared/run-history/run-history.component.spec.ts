import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";
import { RunHistoryComponent } from "./run-history.component";
import { RunHistoryRow } from "./run-history.types";

function row(over: Partial<RunHistoryRow> = {}): RunHistoryRow {
  return {
    id: "1",
    source: "engine",
    strategyName: "ema_spec",
    symbol: "SPY",
    startDate: "2025-01-06",
    endDate: "2025-01-10",
    executedAt: "2026-05-19T02:49:00Z",
    totalTrades: 1,
    totalPnl: 9.0,
    hasSyntheticExit: false,
    leanRunId: null,
    ...over,
  };
}

async function renderWith(
  rows: RunHistoryRow[],
): Promise<ComponentFixture<RunHistoryComponent>> {
  await TestBed.configureTestingModule({
    imports: [RunHistoryComponent],
    providers: [provideZonelessChangeDetection()],
  }).compileComponents();
  const fixture = TestBed.createComponent(RunHistoryComponent);
  fixture.componentRef.setInput("rows", rows);
  fixture.detectChanges();
  return fixture;
}

function text(fixture: ComponentFixture<RunHistoryComponent>): string {
  return (fixture.nativeElement as HTMLElement).textContent ?? "";
}

describe("RunHistoryComponent", () => {
  it("renders one row per input item", async () => {
    const fixture = await renderWith([
      row({ id: "1" }),
      row({ id: "2", source: "lean-sidecar", leanRunId: "ui_run_abc" }),
    ]);
    const rows = (fixture.nativeElement as HTMLElement).querySelectorAll("tbody tr");
    expect(rows).toHaveLength(2);
  });

  it("shows engine badges with human-readable labels", async () => {
    const fixture = await renderWith([
      row({ id: "1", source: "engine" }),
      row({ id: "2", source: "strategy-lab" }),
      row({ id: "3", source: "lean-sidecar" }),
    ]);
    const t = text(fixture);
    expect(t).toContain("Engine Lab");
    expect(t).toContain("Strategy Lab");
    expect(t).toContain("LEAN");
  });

  it("renders strategy name, symbol, trade count, and PnL for each row", async () => {
    const fixture = await renderWith([
      row({ strategyName: "ema_crossover", symbol: "SPY", totalTrades: 3, totalPnl: 42.5 }),
    ]);
    const t = text(fixture);
    expect(t).toContain("ema_crossover");
    expect(t).toContain("SPY");
    expect(t).toContain("3");
    expect(t).toMatch(/\$42\.50/);
  });

  it("dashes out symbol when null", async () => {
    const fixture = await renderWith([row({ symbol: null })]);
    expect(text(fixture)).toContain("—");
  });

  it("flags synthetic-exit runs with an 'open-at-end' badge", async () => {
    const fixture = await renderWith([row({ hasSyntheticExit: true })]);
    expect(text(fixture).toLowerCase()).toContain("open at end");
  });

  it("renders an empty state when rows is empty", async () => {
    const fixture = await renderWith([]);
    expect(text(fixture).toLowerCase()).toContain("no runs");
  });
});
