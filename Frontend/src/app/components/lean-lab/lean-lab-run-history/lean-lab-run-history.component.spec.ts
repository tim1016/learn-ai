import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { describe, expect, it, vi } from "vitest";
import type { RunSummary } from "../../../services/lean-sidecar.types";
import { LeanLabRunHistoryComponent } from "./lean-lab-run-history.component";

function makeRun(overrides: Partial<RunSummary> = {}): RunSummary {
  return {
    run_id: "ui_run_test",
    symbol: "SPY",
    requested_start_ms_utc: 1_736_121_600_000,
    requested_end_ms_utc: 1_736_467_200_000,
    started_at_ms: 1_736_121_650_000,
    finished_at_ms: 1_736_121_700_000,
    exit_code: 0,
    algorithm_source_kind: "trusted_sample",
    exit_clean: true,
    is_clean: true,
    ...overrides,
  };
}

async function renderWith(props: {
  runs: RunSummary[];
  selectedRunId?: string | null;
  loading?: boolean;
  truncated?: boolean;
}): Promise<ComponentFixture<LeanLabRunHistoryComponent>> {
  await TestBed.configureTestingModule({
    imports: [LeanLabRunHistoryComponent],
    providers: [provideZonelessChangeDetection()],
  }).compileComponents();
  const fixture = TestBed.createComponent(LeanLabRunHistoryComponent);
  fixture.componentRef.setInput("runs", props.runs);
  fixture.componentRef.setInput("selectedRunId", props.selectedRunId ?? null);
  fixture.componentRef.setInput("loading", props.loading ?? false);
  fixture.componentRef.setInput("truncated", props.truncated ?? false);
  fixture.detectChanges();
  return fixture;
}

describe("LeanLabRunHistoryComponent", () => {
  it("shows the empty-state copy when no runs are provided", async () => {
    const fixture = await renderWith({ runs: [] });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("No runs yet");
  });

  it("renders one row per run with symbol + run_id", async () => {
    const fixture = await renderWith({
      runs: [
        makeRun({ run_id: "ui_run_one", symbol: "AAPL" }),
        makeRun({ run_id: "ui_run_two", symbol: "MSFT" }),
      ],
    });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("ui_run_one");
    expect(text).toContain("AAPL");
    expect(text).toContain("ui_run_two");
    expect(text).toContain("MSFT");
  });

  it("shows the truncated banner when the server cap is hit", async () => {
    const fixture = await renderWith({ runs: [makeRun()], truncated: true });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("capped");
  });

  it("flags user-provided algorithm runs with a tag", async () => {
    const fixture = await renderWith({
      runs: [makeRun({ algorithm_source_kind: "user_provided" })],
    });
    const text = (fixture.nativeElement as HTMLElement).textContent ?? "";
    expect(text).toContain("custom");
  });

  it("emits runSelected when a row is clicked", async () => {
    const fixture = await renderWith({
      runs: [makeRun({ run_id: "ui_run_clickable" })],
    });
    const onSelect = vi.fn();
    fixture.componentInstance.runSelected.subscribe(onSelect);

    const button = (fixture.nativeElement as HTMLElement).querySelector("button");
    button?.click();

    expect(onSelect).toHaveBeenCalledWith("ui_run_clickable");
  });

  it("disables the click while loading", async () => {
    const fixture = await renderWith({
      runs: [makeRun({ run_id: "ui_run_locked" })],
      loading: true,
    });
    const onSelect = vi.fn();
    fixture.componentInstance.runSelected.subscribe(onSelect);

    const button = (fixture.nativeElement as HTMLElement).querySelector("button");
    button?.click();

    expect(onSelect).not.toHaveBeenCalled();
  });

  it("marks the selected row with aria-current", async () => {
    const fixture = await renderWith({
      runs: [makeRun({ run_id: "ui_run_a" }), makeRun({ run_id: "ui_run_b" })],
      selectedRunId: "ui_run_b",
    });
    const buttons = (fixture.nativeElement as HTMLElement).querySelectorAll("button");
    expect(buttons[0].getAttribute("aria-current")).toBeNull();
    expect(buttons[1].getAttribute("aria-current")).toBe("true");
  });
});
