import { render, screen } from "@testing-library/angular";
import { describe, expect, it } from "vitest";
import { RecencySwimlaneComponent } from "./recency-swimlane.component";
import type { RecencySwimlaneTrade } from "./recency-swimlane-layout";

function trade(overrides: Partial<RecencySwimlaneTrade> = {}): RecencySwimlaneTrade {
  return {
    symbol: "SPY",
    strategyKey: "ema_crossover_2_bps",
    paramsHash: "hash1",
    fingerprint: "fp1",
    entryMs: 1_000,
    exitMs: 2_000,
    pnlPts: 2,
    pnlPct: 0.02,
    quantity: 10,
    pnl: 20,
    holdingSessions: 1,
    sharpe: 1.0,
    studyId: null,
    recencyRunId: 1,
    ...overrides,
  };
}

describe("RecencySwimlaneComponent — virtualization & duration labels (design spec D13, D18-D19)", () => {
  it("virtualizes — a trade entirely outside [windowStartMs, windowEndMs] is not rendered", async () => {
    await render(RecencySwimlaneComponent, {
      inputs: {
        trades: [
          trade({ fingerprint: "in-window", entryMs: 5_000, exitMs: 6_000 }),
          trade({ fingerprint: "outside-window", entryMs: 50_000, exitMs: 60_000 }),
        ],
        windowStartMs: 0,
        windowEndMs: 10_000,
      },
    });

    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("a trade straddling the window edge is still rendered (display-clipped, not dropped)", async () => {
    await render(RecencySwimlaneComponent, {
      inputs: {
        trades: [trade({ fingerprint: "straddling", entryMs: 8_000, exitMs: 20_000 })],
        windowStartMs: 0,
        windowEndMs: 10_000,
      },
    });

    expect(screen.getAllByRole("button")).toHaveLength(1);
  });

  it("labels a wide-enough bar with a visible (non-tooltip) duration text", async () => {
    const view = await render(RecencySwimlaneComponent, {
      inputs: { trades: [trade({ entryMs: 0, exitMs: 1000, holdingSessions: 3 })] },
    });

    const label = (view.container as HTMLElement).querySelector(".bar-label");
    expect(label?.textContent?.trim()).toBe("3 sessions");
  });

  it("does not show a visible duration label on a too-narrow bar", async () => {
    const view = await render(RecencySwimlaneComponent, {
      inputs: {
        trades: [trade({ entryMs: 499_990, exitMs: 500_000, holdingSessions: 1 })],
        windowStartMs: 0,
        windowEndMs: 1_000_000,
      },
    });

    expect((view.container as HTMLElement).querySelector(".bar-label")).toBeNull();
  });
});
