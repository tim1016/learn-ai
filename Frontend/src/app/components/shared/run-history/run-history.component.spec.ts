import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { describe, expect, it } from "vitest";
import { RunHistoryComponent } from "./run-history.component";
import { RunHistoryRow } from "./run-history.types";

function row(over: Partial<RunHistoryRow> = {}): RunHistoryRow {
  return {
    id: "1",
    source: "engine",
    engine: "PYTHON",
    strategyName: "ema_spec",
    symbol: "SPY",
    startDate: "2025-01-06",
    endDate: "2025-01-10",
    executedAt: Date.UTC(2026, 4, 19, 2, 49),
    totalTrades: 1,
    totalPnl: 9.0,
    hasSyntheticExit: false,
    leanRunId: null,
    dataPolicy: null,
    notes: null,
    commissionPerOrder: null,
    brokeragePolicy: null,
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

  it("labels custom LEAN runs as user-modified algorithms", async () => {
    const fixture = await renderWith([
      row({
        source: "lean-sidecar",
        engine: "LEAN",
        strategyName: "user_provided",
      }),
    ]);

    const t = text(fixture);
    expect(t).toContain("User-modified algorithm");
    expect(t).not.toContain("user_provided");
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

describe("RunHistoryComponent — row click", () => {
  it("emits runSelected with the row id when a data row is clicked", async () => {
    const events: string[] = [];
    const { fixture, component } = await renderComponent({
      rows: [row({ id: "abc" }), row({ id: "xyz" })],
    });
    component.runSelected.subscribe((id) => events.push(id));

    const rows = fixture.nativeElement.querySelectorAll("tbody tr") as NodeListOf<HTMLTableRowElement>;
    rows[1].click();

    expect(events).toEqual(["xyz"]);
  });

  it("does not emit runSelected when the checkbox cell is clicked", async () => {
    const events: string[] = [];
    const { fixture, component } = await renderComponent({
      rows: [row({ id: "abc" })],
      allowCompare: true,
    });
    component.runSelected.subscribe((id) => events.push(id));

    const checkbox = fixture.nativeElement.querySelector(
      'input[type="checkbox"]',
    ) as HTMLInputElement;
    checkbox.click();

    expect(events).toHaveLength(0);
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

describe("RunHistoryComponent — Bars summary column (PR B.3)", () => {
  it("renders 'm/1 → m/15' for a minute-1 → minute-15 DataPolicy", async () => {
    const fixture = await renderWith([
      row({
        id: "1",
        dataPolicy: {
          source: "polygon",
          symbol: "SPY",
          adjusted: true,
          session: "regular",
          input_bars: { timespan: "minute", multiplier: 1 },
          strategy_bars: { timespan: "minute", multiplier: 15 },
          timestamp_policy: "bar_close_ms_utc",
          timezone: "America/New_York",
          provider_kind: "live",
          fixture_id: null,
          fixture_sha256: null,
        },
      }),
    ]);
    expect(text(fixture)).toContain("m/1 → m/15");
  });

  it("collapses to a single token when input_bars and strategy_bars match", async () => {
    const fixture = await renderWith([
      row({
        id: "2",
        dataPolicy: {
          source: "polygon",
          symbol: "SPY",
          adjusted: true,
          session: "regular",
          input_bars: { timespan: "day", multiplier: 1 },
          strategy_bars: { timespan: "day", multiplier: 1 },
          timestamp_policy: "bar_close_ms_utc",
          timezone: "America/New_York",
          provider_kind: "live",
          fixture_id: null,
          fixture_sha256: null,
        },
      }),
    ]);
    const cell = (fixture.nativeElement as HTMLElement).querySelector(".bars-cell")?.textContent ?? "";
    expect(cell.trim()).toBe("d/1");
  });

  it("renders an em-dash when DataPolicy is null (legacy row)", async () => {
    const fixture = await renderWith([row({ id: "3", dataPolicy: null })]);
    const cell = (fixture.nativeElement as HTMLElement).querySelector(".bars-cell")?.textContent ?? "";
    expect(cell.trim()).toBe("—");
  });
});

describe("RunHistoryComponent — inline notes editing (PR B.3)", () => {
  it("starts in display mode with '+ note' when notes is null", async () => {
    const fixture = await renderWith([row({ id: "1", notes: null })]);
    const editBtn = fixture.nativeElement.querySelector(
      'button[data-testid="notes-edit"]',
    ) as HTMLButtonElement;
    expect(editBtn.textContent?.trim()).toBe("+ note");
  });

  it("shows the existing notes value in display mode", async () => {
    const fixture = await renderWith([row({ id: "1", notes: "great Sharpe" })]);
    const editBtn = fixture.nativeElement.querySelector(
      'button[data-testid="notes-edit"]',
    ) as HTMLButtonElement;
    expect(editBtn.textContent?.trim()).toBe("great Sharpe");
  });

  it("emits notesEdited with the new value when Save is clicked", async () => {
    const events: { id: string; notes: string }[] = [];
    const { fixture, component } = await renderComponent({
      rows: [row({ id: "abc", notes: "old" })],
    });
    component.notesEdited.subscribe((e) => events.push(e));

    (fixture.nativeElement.querySelector(
      'button[data-testid="notes-edit"]',
    ) as HTMLButtonElement).click();
    fixture.detectChanges();

    const input = fixture.nativeElement.querySelector(
      'input[data-testid="notes-input"]',
    ) as HTMLInputElement;
    input.value = "new note";
    input.dispatchEvent(new Event("input"));
    fixture.detectChanges();

    (fixture.nativeElement.querySelector(
      'button[data-testid="notes-save"]',
    ) as HTMLButtonElement).click();
    fixture.detectChanges();

    expect(events).toEqual([{ id: "abc", notes: "new note" }]);
  });

  it("cancel button discards the edit without emitting", async () => {
    const events: unknown[] = [];
    const { fixture, component } = await renderComponent({
      rows: [row({ id: "x", notes: "keep" })],
    });
    component.notesEdited.subscribe((e) => events.push(e));

    (fixture.nativeElement.querySelector(
      'button[data-testid="notes-edit"]',
    ) as HTMLButtonElement).click();
    fixture.detectChanges();

    (fixture.nativeElement.querySelector(
      'button[data-testid="notes-cancel"]',
    ) as HTMLButtonElement).click();
    fixture.detectChanges();

    expect(events).toHaveLength(0);
    const editBtn = fixture.nativeElement.querySelector(
      'button[data-testid="notes-edit"]',
    ) as HTMLButtonElement;
    expect(editBtn.textContent?.trim()).toBe("keep");
  });

  it("clicking the notes cell does not trigger row-select", async () => {
    const events: string[] = [];
    const { fixture, component } = await renderComponent({
      rows: [row({ id: "abc", notes: null })],
    });
    component.runSelected.subscribe((id) => events.push(id));

    (fixture.nativeElement.querySelector(
      'button[data-testid="notes-edit"]',
    ) as HTMLButtonElement).click();
    fixture.detectChanges();

    expect(events).toHaveLength(0);
  });
});
