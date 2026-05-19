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

async function renderComponent(inputs: {
  rows: RunHistoryRow[];
  allowCompare?: boolean;
}): Promise<{ fixture: ComponentFixture<RunHistoryComponent>; component: RunHistoryComponent }> {
  await TestBed.configureTestingModule({
    imports: [RunHistoryComponent],
    providers: [provideZonelessChangeDetection()],
  }).compileComponents();
  const fixture = TestBed.createComponent(RunHistoryComponent);
  fixture.componentRef.setInput("rows", inputs.rows);
  if (inputs.allowCompare !== undefined) {
    fixture.componentRef.setInput("allowCompare", inputs.allowCompare);
  }
  fixture.detectChanges();
  return { fixture, component: fixture.componentInstance };
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

describe("RunHistoryComponent — multi-select", () => {
  it("does not render checkboxes when allowCompare is false", async () => {
    const { fixture } = await renderComponent({
      rows: [row({ id: "1" }), row({ id: "2" })],
      allowCompare: false,
    });
    const checkboxes = fixture.nativeElement.querySelectorAll('input[type="checkbox"]');
    expect(checkboxes.length).toBe(0);
  });

  it("renders one checkbox per row when allowCompare is true", async () => {
    const { fixture } = await renderComponent({
      rows: [row({ id: "1" }), row({ id: "2" }), row({ id: "3" })],
      allowCompare: true,
    });
    const checkboxes = fixture.nativeElement.querySelectorAll('input[type="checkbox"]');
    expect(checkboxes.length).toBe(3);
  });

  it("Compare button is disabled when 0 selected, 1 selected, or 3+ selected", async () => {
    const { fixture } = await renderComponent({
      rows: [row({ id: "1" }), row({ id: "2" }), row({ id: "3" })],
      allowCompare: true,
    });
    const checkboxes = fixture.nativeElement.querySelectorAll(
      'input[type="checkbox"]',
    ) as NodeListOf<HTMLInputElement>;
    const compareBtn = fixture.nativeElement.querySelector(
      'button[data-testid="compare-selected"]',
    ) as HTMLButtonElement;

    expect(compareBtn.disabled).toBe(true); // 0 selected

    checkboxes[0].click();
    fixture.detectChanges();
    expect(compareBtn.disabled).toBe(true); // 1 selected

    checkboxes[1].click();
    fixture.detectChanges();
    expect(compareBtn.disabled).toBe(false); // 2 selected — enabled

    checkboxes[2].click();
    fixture.detectChanges();
    expect(compareBtn.disabled).toBe(true); // 3 selected — disabled again
  });

  it("emits compareRequested with the two selected IDs in order checked", async () => {
    const events: { leftId: string; rightId: string }[] = [];
    const { fixture, component } = await renderComponent({
      rows: [row({ id: "a" }), row({ id: "b" }), row({ id: "c" })],
      allowCompare: true,
    });
    component.compareRequested.subscribe((e) => events.push(e));

    const checkboxes = fixture.nativeElement.querySelectorAll(
      'input[type="checkbox"]',
    ) as NodeListOf<HTMLInputElement>;
    checkboxes[2].click(); // id "c" checked first
    fixture.detectChanges();
    checkboxes[0].click(); // id "a" checked second
    fixture.detectChanges();

    const compareBtn = fixture.nativeElement.querySelector(
      'button[data-testid="compare-selected"]',
    ) as HTMLButtonElement;
    compareBtn.click();

    expect(events).toEqual([{ leftId: "c", rightId: "a" }]);
  });

  it("unchecking a row removes it from the selection", async () => {
    const { fixture } = await renderComponent({
      rows: [row({ id: "1" }), row({ id: "2" }), row({ id: "3" })],
      allowCompare: true,
    });
    const checkboxes = fixture.nativeElement.querySelectorAll(
      'input[type="checkbox"]',
    ) as NodeListOf<HTMLInputElement>;
    const compareBtn = fixture.nativeElement.querySelector(
      'button[data-testid="compare-selected"]',
    ) as HTMLButtonElement;

    checkboxes[0].click();
    checkboxes[1].click();
    fixture.detectChanges();
    expect(compareBtn.disabled).toBe(false);

    checkboxes[0].click(); // uncheck
    fixture.detectChanges();
    expect(compareBtn.disabled).toBe(true);
  });

  it("button label includes the current count (e.g. '0 / 2 selected')", async () => {
    const { fixture } = await renderComponent({
      rows: [row({ id: "1" }), row({ id: "2" })],
      allowCompare: true,
    });
    const compareBtn = fixture.nativeElement.querySelector(
      'button[data-testid="compare-selected"]',
    ) as HTMLButtonElement;
    expect(compareBtn.textContent?.trim()).toMatch(/0 \/ 2/);

    const cb = fixture.nativeElement.querySelector('input[type="checkbox"]') as HTMLInputElement;
    cb.click();
    fixture.detectChanges();
    expect(compareBtn.textContent?.trim()).toMatch(/1 \/ 2/);
  });
});
