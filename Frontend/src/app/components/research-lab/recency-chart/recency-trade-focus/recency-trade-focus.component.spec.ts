import { fireEvent, render, screen } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";
import { RecencyTradeFocusComponent } from "./recency-trade-focus.component";
import type { RecencySwimlaneTrade } from "../recency-swimlane/recency-swimlane-layout";

function trade(overrides: Partial<RecencySwimlaneTrade> = {}): RecencySwimlaneTrade {
  return {
    symbol: "SPY",
    strategyKey: "ema_crossover_2_bps",
    paramsHash: "hash1",
    paramsJson: '{"gap_bps":2,"rsi_min":50}',
    fingerprint: "fp1",
    entryMs: Date.UTC(2026, 5, 12, 13, 31),
    exitMs: Date.UTC(2026, 5, 27, 19, 44),
    pnlPts: 2,
    pnlPct: 0.024,
    quantity: 10,
    pnl: 24,
    holdingSessions: 11,
    sharpe: 1.2,
    studyId: 4187,
    recencyRunId: 1,
    ...overrides,
  };
}

describe("RecencyTradeFocusComponent", () => {
  it("shows a placeholder and no delete button when no trade is pinned", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: null } });
    expect.soft(screen.getByText(/click a bar/i), "shows a placeholder when no trade is pinned").not.toBeNull();
    expect.soft(screen.queryByRole("button", { name: /delete run/i }), "does not render a delete button when no trade is pinned").toBeNull();
  });

  it("renders a pinned trade: symbol, strategy, dollar pnl, combo parameters, study link, asset identity, timestamps and the in-sample caption", async () => {
    const { container } = await render(RecencyTradeFocusComponent, { inputs: { trade: trade() } });

    expect.soft(screen.getByText("SPY"), "renders the pinned trade's symbol").not.toBeNull();
    expect.soft(screen.getByText("ema_crossover_2_bps"), "renders the pinned trade's strategy").not.toBeNull();
    expect.soft(screen.getByText(/\+24\.00/), "renders the pinned trade's pnl").not.toBeNull();

    expect.soft(screen.getByText("+24.00"), "renders the dollar pnl without a percent sign — t.pnl is not t.pnlPct: the dollar value").not.toBeNull();
    expect.soft(screen.queryByText("+24.00%"), "renders the dollar pnl without a percent sign — t.pnl is not t.pnlPct: no percent form").toBeNull();

    expect.soft(screen.getByText(/gap_bps/), "renders the parsed combo parameters: gap_bps").not.toBeNull();
    expect.soft(screen.getByText(/rsi_min/), "renders the parsed combo parameters: rsi_min").not.toBeNull();

    const link = screen.getByRole("link", { name: /open run/i }) as HTMLAnchorElement;
    expect.soft(link.getAttribute("href"), "links to the underlying study when a studyId is present").toContain("4187");

    const identity = container.querySelector("app-asset-identity");
    expect.soft(identity, "renders the symbol through the shared asset-identity component: present").not.toBeNull();
    expect
      .soft(identity?.getAttribute("ng-reflect-symbol") ?? identity?.textContent, "renders the symbol through the shared asset-identity component: carries SPY")
      .toContain("SPY");

    const displays = container.querySelectorAll("app-timestamp-display");
    expect.soft(displays.length, "shows the entry and exit timestamps").toBeGreaterThanOrEqual(2);

    expect.soft(screen.getByText(/in-sample selection/i), "shows the in-sample / not-OOS caption: in-sample selection").not.toBeNull();
    expect.soft(screen.getByText(/not.*out-of-sample proof/i), "shows the in-sample / not-OOS caption: not out-of-sample proof").not.toBeNull();
  });

  it("degrades gracefully when paramsJson is missing", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: trade({ paramsJson: undefined }) } });
    expect(screen.getByText(/parameters unavailable/i)).not.toBeNull();
  });

  it("does not render an open-run link when studyId is null", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: trade({ studyId: null }) } });
    expect(screen.queryByRole("link", { name: /open run/i })).toBeNull();
  });

  it("emits deleteRequested with the pinned trade's recencyRunId when Delete run is clicked", async () => {
    const view = await render(RecencyTradeFocusComponent, { inputs: { trade: trade({ recencyRunId: 7 }) } });
    const onDelete = vi.fn();
    view.fixture.componentInstance.deleteRequested.subscribe(onDelete);

    fireEvent.click(screen.getByRole("button", { name: /delete run/i }));

    expect(onDelete).toHaveBeenCalledWith(7);
  });
});
