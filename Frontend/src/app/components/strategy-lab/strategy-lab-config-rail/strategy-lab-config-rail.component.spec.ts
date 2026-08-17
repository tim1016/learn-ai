import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { describe, expect, it, vi } from "vitest";

import type { DataPolicy } from "../../../models/data-policy";
import type { StrategyInfo } from "../strategy-lab.models";
import { StrategyLabConfigRailComponent } from "./strategy-lab-config-rail.component";

const STRATEGY: StrategyInfo = {
  name: "ema_crossover_signal",
  display_name: "EMA crossover",
  description: "EMA signal",
  supported_resolutions: ["minute"],
  strategy_bars: { timespan: "minute", multiplier: 15, parameter: null },
  params_schema: {
    properties: {
      symbol: { type: "string", default: "SPY" },
      fast: { type: "integer", default: 12, minimum: 2 },
      slow: { type: "integer", default: 26, minimum: 3 },
    },
  },
};

const DATA_POLICY: DataPolicy = {
  source: "polygon",
  symbol: "SPY",
  adjusted: false,
  session: "extended",
  input_bars: { timespan: "minute", multiplier: 5 },
  strategy_bars: { timespan: "minute", multiplier: 15 },
  timestamp_policy: "bar_close_ms_utc",
  timezone: "America/New_York",
  provider_kind: "fixture",
  fixture_id: "spy-bars-v1",
  fixture_sha256: "a".repeat(64),
};

async function createRail(collapsed = false): Promise<ComponentFixture<StrategyLabConfigRailComponent>> {
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [StrategyLabConfigRailComponent],
    providers: [provideZonelessChangeDetection()],
  }).compileComponents();
  const fixture = TestBed.createComponent(StrategyLabConfigRailComponent);
  fixture.componentRef.setInput("collapsed", collapsed);
  fixture.componentRef.setInput("engine", "both");
  fixture.componentRef.setInput("dataPolicy", DATA_POLICY);
  fixture.componentRef.setInput("range", {
    symbol: "SPY",
    from: "2026-01-05",
    to: "2026-01-30",
    resolution: "minute",
    autoFetch: true,
  });
  fixture.componentRef.setInput("strategies", [STRATEGY]);
  fixture.componentRef.setInput("selectedStrategyName", STRATEGY.name);
  fixture.componentRef.setInput("paramValues", { fast: 12, slow: 26 });
  fixture.detectChanges();
  return fixture;
}

describe("StrategyLabConfigRailComponent", () => {
  it("renders exactly the four primary flat inputs and keeps parameters in Advanced", async () => {
    const fixture = await createRail();
    const root = fixture.nativeElement as HTMLElement;

    expect(root.textContent).toContain("Engine");
    expect(root.textContent).toContain("Instrument");
    expect(root.textContent).toContain("Time window");
    expect(root.textContent).toContain("Strategy");
    expect(root.querySelectorAll("app-config-section")).toHaveLength(0);
    expect(root.querySelector(".availability-strip")).toBeNull();
    expect(root.querySelector(".availability-legend")).toBeNull();

    const advanced = root.querySelector<HTMLDetailsElement>("details.advanced");
    expect(advanced?.open).toBe(false);
    if (!advanced) throw new Error("Advanced disclosure is missing");
    advanced.open = true;
    fixture.detectChanges();
    expect(advanced?.textContent).toContain("5 minutes · extended session · fixture spy-bars-v1");
    expect(advanced?.textContent).toContain("Strategy parameters");
    expect(advanced?.textContent).toContain("Execution");
    expect(advanced?.textContent).toContain("LEAN launcher");
  });

  it("shows the restored strategy when it is not the first available option", async () => {
    const fixture = await createRail();
    fixture.componentRef.setInput("strategies", [{
      ...STRATEGY,
      name: "deployment_validation",
      display_name: "Deployment Validation",
    }, STRATEGY]);
    fixture.detectChanges();

    const picker = (fixture.nativeElement as HTMLElement)
      .querySelector<HTMLSelectElement>("#strategy-picker");
    expect(picker?.value).toBe("ema_crossover_signal");
    expect(picker?.selectedOptions[0]?.textContent).toContain("EMA crossover");
  });

  it("collapses vertically while retaining the full side-rail width", async () => {
    const fixture = await createRail(true);
    const root = fixture.nativeElement as HTMLElement;

    expect(root.querySelector(".config-rail")).toBeNull();
    expect(root.querySelector(".config-strip")).not.toBeNull();
    expect(root.textContent).toContain("Configuration");
    expect(root.textContent).toContain("Both");
    expect(root.textContent).toContain("SPY");
    expect(root.textContent).toContain("EMA crossover");
    expect(root.querySelector(".config-strip__facts")).not.toBeNull();
    expect(root.querySelector("select")).toBeNull();
    expect(root.querySelector("app-instrument-card")).toBeNull();
    expect(root.querySelectorAll(".config-strip button")).toHaveLength(1);
    expect(root.querySelector<HTMLButtonElement>("[aria-label='Expand configuration']")).not.toBeNull();
  });

  it("keeps the launcher recovery action clickable when LEAN is blocked", async () => {
    const fixture = await createRail();
    const launcherCheckRequested = vi.fn();
    const runRequested = vi.fn();
    fixture.componentRef.setInput("launcherBlocksRun", true);
    fixture.componentInstance.launcherCheckRequested.subscribe(launcherCheckRequested);
    fixture.componentInstance.runRequested.subscribe(runRequested);
    fixture.detectChanges();

    const button = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>(".run-button");
    expect(button?.textContent).toContain("Check launcher");
    expect(button?.disabled).toBe(false);
    button?.click();

    expect(launcherCheckRequested).toHaveBeenCalledOnce();
    expect(runRequested).not.toHaveBeenCalled();
  });

  it("disables rerun when the restored strategy is no longer selectable", async () => {
    const fixture = await createRail();
    fixture.componentRef.setInput("launcherBlocksRun", false);
    fixture.componentRef.setInput("selectedStrategyName", "retired_strategy");
    fixture.detectChanges();

    const button = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>(".run-button");
    expect(button?.disabled).toBe(true);
  });

  it("does not offer launcher recovery for a configuration that cannot be rerun", async () => {
    const fixture = await createRail();
    fixture.componentRef.setInput("launcherBlocksRun", true);
    fixture.componentRef.setInput("runBlocked", true);
    fixture.componentRef.setInput("selectedStrategyName", "retired_strategy");
    fixture.detectChanges();

    const button = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>(".run-button");
    expect(button?.textContent).toContain("Check launcher");
    expect(button?.disabled).toBe(true);
  });

  it("keeps declared strategy parameters editable in LEAN mode", async () => {
    const fixture = await createRail();
    fixture.componentRef.setInput("engine", "lean");
    fixture.detectChanges();

    const advanced = (fixture.nativeElement as HTMLElement)
      .querySelector<HTMLDetailsElement>("details.advanced");
    if (!advanced) throw new Error("Advanced disclosure is missing");
    advanced.open = true;
    fixture.detectChanges();

    expect(advanced.textContent).toContain("Strategy parameters");
    expect(advanced.querySelectorAll(".advanced-params input")).toHaveLength(3);
    expect(advanced.textContent).toContain("Fills and fees are defined by the aligned LEAN template.");
  });
});
