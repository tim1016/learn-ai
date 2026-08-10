import { render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";

import type { RunVerdict } from "../../../api/run-verdict.types";
import { EvidenceGradeComponent } from "./evidence-grade.component";

function verdict(overrides: Partial<RunVerdict> = {}): RunVerdict {
  return {
    verdict_version: 2,
    status: "complete",
    engine: "python",
    generated_at_ms: 1_700_000_000_000,
    composite: 76,
    grade: "A",
    evidence_action: "Continue forward and out-of-sample validation",
    signal: "Paper-trade",
    headline: "Strong backtest evidence; continue forward and out-of-sample validation.",
    red_flags: [],
    dimensions: [],
    missing_metrics: [],
    missing_required_metrics: [],
    missing_required_evidence: [],
    available_required_metrics: 17,
    required_metrics: 17,
    normalized_weights: false,
    cleanliness: null,
    ...overrides,
  };
}

describe("EvidenceGradeComponent", () => {
  it("renders Python-authored evidence-grade copy without the legacy deployment signal", async () => {
    await render(EvidenceGradeComponent, { inputs: { verdict: verdict() } });

    expect(screen.getByText("Backtest Evidence Grade")).toBeTruthy();
    expect(screen.getByText("A")).toBeTruthy();
    expect(screen.getByText("Frozen policy score: 76/100")).toBeTruthy();
    expect(screen.getByText(/Next validation step: Continue forward/)).toBeTruthy();
    expect(screen.queryByText("Paper-trade")).toBeNull();
    expect(screen.getByText(/does not authorize trading/)).toBeTruthy();
  });

  it("shows exact incomplete evidence with its producer-authored unavailable reason", async () => {
    await render(EvidenceGradeComponent, {
      inputs: {
        verdict: verdict({
          status: "incomplete",
          composite: null,
          grade: null,
          evidence_action: null,
          signal: null,
          headline: "Backtest Evidence Grade incomplete — 16/17 required metrics available.",
          missing_required_metrics: ["Return Quality: Sortino ratio"],
          missing_required_evidence: [
            {
              key: "sortino",
              label: "Return Quality: Sortino ratio",
              producer: "platform",
              reason: "No negative returns this window.",
            },
          ],
          available_required_metrics: 16,
        }),
      },
    });

    expect(screen.getByText("16/17")).toBeTruthy();
    expect(screen.getByText(/no composite, grade, or action was produced/)).toBeTruthy();
    expect(screen.getByText("Return Quality: Sortino ratio")).toBeTruthy();
    expect(screen.getByText("No negative returns this window.")).toBeTruthy();
    expect(screen.queryByText("A")).toBeNull();
  });

  it("renders a failed run as a failure rather than an F-grade conclusion", async () => {
    await render(EvidenceGradeComponent, {
      inputs: {
        verdict: verdict({
          status: "failed",
          composite: null,
          grade: null,
          evidence_action: null,
          signal: null,
          headline: "Run failed — LEAN run failed before producing normalized results.",
          available_required_metrics: 0,
        }),
      },
    });

    expect(screen.getByText("Run failed")).toBeTruthy();
    expect(screen.getByText(/No research conclusion was produced/)).toBeTruthy();
    expect(screen.queryByText("F")).toBeNull();
  });

  it("treats a historical verdict without a persisted status as legacy rather than complete", async () => {
    await render(EvidenceGradeComponent, {
      inputs: { verdict: verdict({ status: undefined, grade: "A", composite: 76 }) },
    });

    expect(screen.getByText(/did not record a status/)).toBeTruthy();
    expect(screen.queryByText("Frozen policy score: 76/100")).toBeNull();
    expect(document.querySelector("[data-status='legacy']")).toBeTruthy();
  });

  it("renders producer-authored unavailable evidence instead of suppressing it", async () => {
    await render(EvidenceGradeComponent, {
      inputs: {
        verdict: verdict({
          status: "unavailable",
          composite: null,
          grade: null,
          evidence_action: null,
          signal: null,
          available_required_metrics: 0,
          missing_required_evidence: [{
            key: "sharpe",
            label: "Return Quality: Sharpe ratio",
            producer: "platform",
            reason: "The persisted equity curve is unavailable.",
          }],
        }),
      },
    });

    expect(screen.getByText(/Required evidence is unavailable/)).toBeTruthy();
    expect(screen.getByText("Return Quality: Sharpe ratio")).toBeTruthy();
    expect(screen.getByText("The persisted equity curve is unavailable.")).toBeTruthy();
  });
});
