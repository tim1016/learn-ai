import { provideZonelessChangeDetection } from "@angular/core";
import { ComponentFixture, TestBed } from "@angular/core/testing";
import { describe, expect, it, vi } from "vitest";

import type { DataPolicy } from "../../../models/data-policy";
import type { EngineChoice, StrategyInfo } from "../strategy-lab.models";
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from "../../../shared/ticker-catalog/testing/fake-ticker-catalog";
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

async function createRail(
  collapsed = false,
  engine: EngineChoice = "both",
): Promise<ComponentFixture<StrategyLabConfigRailComponent>> {
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [StrategyLabConfigRailComponent],
    providers: [
      provideZonelessChangeDetection(),
      // The card reads the lake catalog on init; without this the real
      // service is constructed and fires a doomed XHR on every run.
      provideFakeTickerCatalog(
        fakeTickerCatalog([{ symbol: "SPY", name: "SPDR S&P 500 ETF Trust", exchange: "ARCA" }]),
      ),
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(StrategyLabConfigRailComponent);
  fixture.componentRef.setInput("collapsed", collapsed);
  fixture.componentRef.setInput("engine", engine);
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
    // A launcher that has passed its check lives in Advanced; an unchecked one is on the rail (#1976).
    fixture.componentRef.setInput("launcherStatus", "ready");
    fixture.detectChanges();
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

  it("shows a blocked launcher's status and detail outside the collapsed Advanced block", async () => {
    // #1976: with the launcher down the primary button became "Check launcher"
    // while the status, the diagnose detail and the start command stayed inside
    // <details class="advanced">, collapsed, so the rail never said why.
    const fixture = await createRail();
    fixture.componentRef.setInput("launcherStatus", "blocked");
    fixture.componentRef.setInput("launcherDetail", "launcher_unreachable: connection refused");
    fixture.componentRef.setInput("launcherCommand", "uvicorn app.lean_sidecar.launcher.app:app");
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;

    const launcher = root.querySelector<HTMLElement>(".lean-launcher");
    expect(launcher).not.toBeNull();
    expect(launcher?.closest("details")).toBeNull();
    expect(launcher?.textContent).toContain("blocked");
    expect(launcher?.textContent).toContain("launcher_unreachable: connection refused");
    expect(launcher?.textContent).toContain("uvicorn app.lean_sidecar.launcher.app:app");
    expect(root.querySelectorAll(".lean-launcher")).toHaveLength(1);
  });

  it("keeps a ready launcher's status inside the Advanced block", async () => {
    const fixture = await createRail();
    fixture.componentRef.setInput("launcherStatus", "ready");
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;

    const launcher = root.querySelector<HTMLElement>(".lean-launcher");
    expect(launcher).not.toBeNull();
    expect(launcher?.closest("details.advanced")).not.toBeNull();
    expect(root.querySelectorAll(".lean-launcher")).toHaveLength(1);
  });

  it("keeps the launcher recovery action clickable when LEAN is blocked", async () => {
    const fixture = await createRail();
    const launcherCheckRequested = vi.fn();
    const runRequested = vi.fn();
    fixture.componentRef.setInput("launcherStatus", "blocked");
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

  it("blocks Run while another tab's backtest is busy, but keeps launcher recovery clickable", async () => {
    const fixture = await createRail();
    fixture.componentRef.setInput("engineBusy", true);
    fixture.componentRef.setInput("launcherStatus", "blocked");
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;

    const launcher = root.querySelector<HTMLButtonElement>(".run-button");
    expect(launcher?.textContent).toContain("Check launcher");
    expect(launcher?.disabled).toBe(false);

    fixture.componentRef.setInput("launcherStatus", "ready");
    fixture.detectChanges();
    const run = root.querySelector<HTMLButtonElement>(".run-button");
    expect(run?.textContent).toContain("Run validation");
    expect(run?.disabled).toBe(true);
  });

  it("disables rerun when the restored strategy is no longer selectable", async () => {
    const fixture = await createRail();
    fixture.componentRef.setInput("launcherStatus", "ready");
    fixture.componentRef.setInput("selectedStrategyName", "retired_strategy");
    fixture.detectChanges();

    const button = (fixture.nativeElement as HTMLElement).querySelector<HTMLButtonElement>(".run-button");
    expect(button?.disabled).toBe(true);
  });

  it("does not offer launcher recovery for a configuration that cannot be rerun", async () => {
    const fixture = await createRail();
    fixture.componentRef.setInput("launcherStatus", "blocked");
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

  it("offers the QCAlgorithm editor only when an engine that runs LEAN is selected", async () => {
    const fixture = await createRail(false, "lean");
    const requested = vi.fn();
    fixture.componentInstance.leanSourceRequested.subscribe(requested);
    const root = fixture.nativeElement as HTMLElement;

    const button = root.querySelector<HTMLButtonElement>("button[aria-label='Edit QCAlgorithm source']");
    if (!button) throw new Error("QCAlgorithm editor trigger is missing");
    button.click();

    expect(requested).toHaveBeenCalled();
  });

  it("hides the QCAlgorithm editor for the Python engine", async () => {
    const fixture = await createRail(false, "python");

    expect((fixture.nativeElement as HTMLElement)
      .querySelector("button[aria-label='Edit QCAlgorithm source']")).toBeNull();
  });

  it("hides the QCAlgorithm editor in Both mode, which cannot carry a custom source", async () => {
    const fixture = await createRail(false, "both");
    fixture.componentRef.setInput("launcherStatus", "ready");
    fixture.detectChanges();
    const root = fixture.nativeElement as HTMLElement;

    // `StrategyLabRunner.run()` routes `both` through `runPython()`, whose
    // payload has no custom-source field — the compatibility companion runs
    // the registered twin, so offering the editor here promised an execution
    // that silently discarded the operator's QCAlgorithm.
    expect(root.querySelector("button[aria-label='Edit QCAlgorithm source']")).toBeNull();
    // The LEAN runtime still has to be reachable for the companion run, so
    // the launcher panel stays.
    const advanced = root.querySelector<HTMLDetailsElement>("details.advanced");
    if (!advanced) throw new Error("Advanced disclosure is missing");
    advanced.open = true;
    fixture.detectChanges();
    expect(advanced.textContent).toContain("LEAN launcher");
  });
});
