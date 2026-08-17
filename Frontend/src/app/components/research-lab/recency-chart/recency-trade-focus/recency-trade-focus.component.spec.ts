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
  it("shows a placeholder when no trade is pinned", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: null } });
    expect(screen.getByText(/click a bar/i)).not.toBeNull();
  });

  it("renders the pinned trade's symbol, strategy, and pnl", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: trade() } });

    expect(screen.getByText("SPY")).not.toBeNull();
    expect(screen.getByText("ema_crossover_2_bps")).not.toBeNull();
    expect(screen.getByText(/\+24\.00/)).not.toBeNull();
  });

  it("renders the dollar pnl without a percent sign — t.pnl is not t.pnlPct", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: trade({ pnl: 24 }) } });

    expect(screen.getByText("+24.00")).not.toBeNull();
    expect(screen.queryByText("+24.00%")).toBeNull();
  });

  it("renders the parsed combo parameters", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: trade() } });

    expect(screen.getByText(/gap_bps/)).not.toBeNull();
    expect(screen.getByText(/rsi_min/)).not.toBeNull();
  });

  it("degrades gracefully when paramsJson is missing", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: trade({ paramsJson: undefined }) } });
    expect(screen.getByText(/parameters unavailable/i)).not.toBeNull();
  });

  it("links to the underlying study when a studyId is present", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: trade({ studyId: 4187 }) } });

    const link = screen.getByRole("link", { name: /open run/i }) as HTMLAnchorElement;
    expect(link.getAttribute("href")).toContain("4187");
  });

  it("does not render an open-run link when studyId is null", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: trade({ studyId: null }) } });
    expect(screen.queryByRole("link", { name: /open run/i })).toBeNull();
  });

  it("shows the in-sample / not-OOS caption", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: trade() } });
    expect(screen.getByText(/in-sample selection/i)).not.toBeNull();
    expect(screen.getByText(/not.*out-of-sample proof/i)).not.toBeNull();
  });

  it("emits deleteRequested with the pinned trade's recencyRunId when Delete run is clicked", async () => {
    const view = await render(RecencyTradeFocusComponent, { inputs: { trade: trade({ recencyRunId: 7 }) } });
    const onDelete = vi.fn();
    view.fixture.componentInstance.deleteRequested.subscribe(onDelete);

    fireEvent.click(screen.getByRole("button", { name: /delete run/i }));

    expect(onDelete).toHaveBeenCalledWith(7);
  });

  it("does not render a delete button when no trade is pinned", async () => {
    await render(RecencyTradeFocusComponent, { inputs: { trade: null } });
    expect(screen.queryByRole("button", { name: /delete run/i })).toBeNull();
  });
});
