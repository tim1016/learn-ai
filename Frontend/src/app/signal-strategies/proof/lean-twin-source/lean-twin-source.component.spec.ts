import { provideZonelessChangeDetection } from "@angular/core";
import { render, screen } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";

import { LeanSourceService } from "../../../services/lean-source.service";
import { LeanTwinSourceComponent } from "./lean-twin-source.component";

async function renderViewer(getStrategySource: ReturnType<typeof vi.fn>, strategyKey: string) {
  return render(LeanTwinSourceComponent, {
    inputs: { strategyKey },
    providers: [provideZonelessChangeDetection(), { provide: LeanSourceService, useValue: { getStrategySource } }],
  });
}

describe("LeanTwinSourceComponent", () => {
  it("shows the registered twin and its source hash", async () => {
    await renderViewer(vi.fn(async () => ({
      kind: "available" as const,
      source: {
        strategy_name: "rsi_mean_reversion", template: "rsi_mean_reversion", language: "python",
        source: "class RsiAlgorithm(QCAlgorithm): pass", source_sha256: "c".repeat(64),
      },
    })), "rsi_mean_reversion");

    expect(await screen.findByText(/class RsiAlgorithm/)).toBeTruthy();
    expect(screen.getByTitle("Registered source SHA-256").textContent).toBe("c".repeat(64));
    // A twin that loaded successfully must not also read as either the
    // "no twin registered" status or a lookup-failure alert.
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("states that a strategy has no registered twin rather than reporting a failure", async () => {
    await renderViewer(vi.fn(async () => ({
      kind: "unregistered" as const,
      detail: "Strategy 'sma_crossover' has no registered LEAN validation source",
    })), "sma_crossover");

    // The absence of a twin is a fact, announced politely (role="status"),
    // never as role="alert" — that distinction is the point of this component.
    // Wait on the specific text first: the loading placeholder is also
    // role="status", so a bare findByRole("status") would resolve too early.
    await screen.findByText(/has no registered LEAN validation source/);
    expect(screen.getByRole("status").textContent).toContain(
      "has no registered LEAN validation source",
    );
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("raises a real lookup failure as an alert", async () => {
    await renderViewer(vi.fn(async () => ({
      kind: "unavailable" as const,
      detail: "The registered QCAlgorithm source could not be loaded.",
    })), "rsi_mean_reversion");

    expect((await screen.findByRole("alert")).textContent)
      .toContain("The registered QCAlgorithm source could not be loaded.");
    // A genuine failure must not also present as the calm "no twin" status.
    expect(screen.queryByRole("status")).toBeNull();
  });
});
