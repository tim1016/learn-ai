import { signal } from "@angular/core";
import { Router } from "@angular/router";
import { fireEvent, render, screen } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";

import type { AccountDeskHoldingRow } from "./account-desk-holdings-store.service";
import { AccountDeskHoldingsStore } from "./account-desk-holdings-store.service";
import { makeAccountSummary, makePosition, makePositionOwner, makeUnattributedHoldingBlocker } from "./account-desk-holdings.fixtures";
import { AccountDeskBrokerSnapshotComponent } from "./account-desk-broker-snapshot.component";

function makeStore(overrides: Partial<ReturnType<typeof baseStore>> = {}) {
  return { ...baseStore(), ...overrides };
}

function baseStore() {
  const position = makePosition();
  const row: AccountDeskHoldingRow = {
    position,
    owner: makePositionOwner(),
    pnl: { account_id: position.account_id, con_id: position.con_id, daily_pnl: 7, unrealized_pnl: 12, realized_pnl: 0, market_value: 1_020, position: 2, ts_ms: 1_780_000_003_000 },
    blockers: [makeUnattributedHoldingBlocker(position.con_id)],
  };
  return {
    loading: signal(false), error: signal<unknown>(null), unavailableMessage: signal<string | null>(null), hasLastGood: signal(true), showingStaleLastGood: signal(false),
    account: signal<ReturnType<typeof makeAccountSummary> | null>(makeAccountSummary()), headlineMetrics: signal({ equity: 10_000, cash: 1_000, buyingPower: 20_000, dayPnl: 25, openPositions: 1 }),
    rows: signal<readonly AccountDeskHoldingRow[]>([row]), rowsForLens: vi.fn(() => [row]), retry: vi.fn(),
  };
}

async function setup(store = makeStore(), lens: "trader" | "operator" = "trader") {
  const router = { navigate: vi.fn().mockResolvedValue(true) };
  await render(AccountDeskBrokerSnapshotComponent, {
    inputs: { lens },
    providers: [{ provide: AccountDeskHoldingsStore, useValue: store }, { provide: Router, useValue: router }],
  });
  return { router, store };
}

describe("AccountDeskBrokerSnapshotComponent", () => {
  it("preserves the complete broker snapshot, holding ownership, and anchored guidance", async () => {
    await setup();

    expect(await screen.findByText("Broker snapshot")).toBeTruthy();
    expect(screen.getByText("Cash")).toBeTruthy();
    expect(screen.getByText("Net liquidation")).toBeTruthy();
    expect(screen.getByText("Buying power")).toBeTruthy();
    expect(screen.getByText("$10,000.00")).toBeTruthy();
    expect(screen.getByText("$1,020.00")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Show guidance for SPY" }));
    expect(screen.getByText("Foreign or unclaimed broker position")).toBeTruthy();
  });

  it("uses operator-scoped holding guidance without changing the broker snapshot", async () => {
    const { store } = await setup(undefined, "operator");

    expect(store.rowsForLens).toHaveBeenCalledWith("operator");
    expect(screen.getByText("Operator evidence for the selected account")).toBeTruthy();
  });

  it("fails closed instead of presenting another account's holdings", async () => {
    await setup(makeStore({ account: signal(null), rows: signal([]), hasLastGood: signal(false), unavailableMessage: signal("The connected broker session is attached to a different account. Live holdings are unavailable.") }));

    expect(await screen.findByText(/different account/)).toBeTruthy();
    expect(screen.queryByText("Bot alpha")).toBeNull();
  });
});
