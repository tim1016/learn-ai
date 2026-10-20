import { fireEvent, render, screen } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";
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

describe("RecencySwimlaneComponent", () => {
  it("renders one lane per distinct symbol", async () => {
    await render(RecencySwimlaneComponent, {
      inputs: {
        trades: [
          trade({ symbol: "SPY", fingerprint: "a" }),
          trade({ symbol: "AAPL", fingerprint: "b" }),
        ],
      },
    });

    expect(screen.getByTestId("lane-SPY")).not.toBeNull();
    expect(screen.getByTestId("lane-AAPL")).not.toBeNull();
  });

  it("orders lanes by recency — freshest trade's symbol first in the DOM", async () => {
    const view = await render(RecencySwimlaneComponent, {
      inputs: {
        trades: [
          trade({ symbol: "OLD", fingerprint: "a", entryMs: 1_000, exitMs: 1_500 }),
          trade({ symbol: "FRESH", fingerprint: "b", entryMs: 9_000, exitMs: 9_500 }),
        ],
      },
    });

    const lanes = Array.from(
      (view.container as HTMLElement).querySelectorAll<HTMLElement>("[data-testid^='lane-']"),
    );
    expect(lanes.map((l) => l.dataset["testid"])).toEqual(["lane-FRESH", "lane-OLD"]);
  });

  it("renders each trade as an accessible, named button", async () => {
    await render(RecencySwimlaneComponent, {
      inputs: {
        trades: [trade({ strategyKey: "ema_crossover_2_bps", pnl: 20 })],
      },
    });

    const button = screen.getByRole("button", { name: /ema_crossover_2_bps/i });
    expect(button).not.toBeNull();
  });

  it.each([
    ["clicked", "clicked-one", (button: HTMLElement) => fireEvent.click(button)],
    [
      "activated with the Enter key",
      "keyboard-one",
      (button: HTMLElement) => fireEvent.keyDown(button, { key: "Enter" }),
    ],
  ])("emits tradeSelected with the trade when it is %s", async (_name, fingerprint, activate) => {
    const t = trade({ fingerprint });
    const view = await render(RecencySwimlaneComponent, { inputs: { trades: [t] } });
    const onSelect = vi.fn();
    view.fixture.componentInstance.tradeSelected.subscribe(onSelect);

    activate(screen.getByRole("button"));
    await view.fixture.whenStable();

    expect(onSelect).toHaveBeenCalledWith(expect.objectContaining({ fingerprint }));
  });

  it("renders nothing and does not throw for an empty trade list", async () => {
    await render(RecencySwimlaneComponent, { inputs: { trades: [] } });

    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });
});
