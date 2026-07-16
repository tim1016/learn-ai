import { signal } from "@angular/core";
import { Router } from "@angular/router";
import { fireEvent, render, screen } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";

import type { AccountDeskHoldingRow } from "./account-desk-holdings-store.service";
import { AccountDeskHoldingsStore } from "./account-desk-holdings-store.service";
import {
  makeAccountSummary,
  makePosition,
  makePositionOwner,
  makeUnattributedHoldingBlocker,
} from "./account-desk-holdings.fixtures";
import { AccountDeskTraderHoldingsComponent } from "./account-desk-trader-holdings.component";

function makeStore(overrides: Partial<ReturnType<typeof baseStore>> = {}) {
  return { ...baseStore(), ...overrides };
}

function baseStore() {
  const position = makePosition();
  const row: AccountDeskHoldingRow = {
    position,
    owner: makePositionOwner(),
    pnl: {
      account_id: position.account_id,
      con_id: position.con_id,
      daily_pnl: 7,
      unrealized_pnl: 12,
      realized_pnl: 0,
      market_value: 1_020,
      position: 2,
      ts_ms: 1_780_000_003_000,
    },
    blockers: [makeUnattributedHoldingBlocker(position.con_id)],
  };
  return {
    loading: signal(false),
    error: signal<unknown>(null),
    unavailableMessage: signal<string | null>(null),
    hasLastGood: signal(true),
    showingStaleLastGood: signal(false),
    account: signal<ReturnType<typeof makeAccountSummary> | null>(
      makeAccountSummary(),
    ),
    headlineMetrics: signal({
      equity: 10_000,
      cash: 1_000,
      buyingPower: 20_000,
      dayPnl: 25,
      openPositions: 1,
    }),
    rows: signal<readonly AccountDeskHoldingRow[]>([row]),
    retry: vi.fn(),
  };
}

async function setup(store = makeStore()) {
  const router = { navigate: vi.fn().mockResolvedValue(true) };
  await render(AccountDeskTraderHoldingsComponent, {
    providers: [
      { provide: AccountDeskHoldingsStore, useValue: store },
      { provide: Router, useValue: router },
    ],
  });
  return { router, store };
}

describe("AccountDeskTraderHoldingsComponent", () => {
  it("renders broker-attested balances, live holding P&L, backend owner, and anchored warning guidance", async () => {
    await setup();

    expect(await screen.findByText("Broker-attested balances")).toBeTruthy();
    expect(screen.getByText("$10,000.00")).toBeTruthy();
    expect(screen.getByText("$1,020.00")).toBeTruthy();
    expect(screen.getByText("Bot alpha")).toBeTruthy();
    fireEvent.click(
      screen.getByRole("button", { name: "Show guidance for SPY" }),
    );
    expect(
      screen.getByText("Foreign or unclaimed broker position"),
    ).toBeTruthy();
  });

  it("follows the backend-authored warning move without constructing a cure from the owner class", async () => {
    const { router } = await setup();

    fireEvent.click(
      await screen.findByRole("button", { name: "Show guidance for SPY" }),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "Open IBKR setup guide" }),
    );

    expect(router.navigate).toHaveBeenCalledWith(["/docs/ibkr-setup-guide"], {
      fragment: undefined,
    });
  });

  it("renders an explicit unavailable state rather than foreign-account holdings", async () => {
    const store = makeStore({
      account: signal(null),
      rows: signal([]),
      hasLastGood: signal(false),
      unavailableMessage: signal(
        "The connected broker session is attached to a different account. Live holdings are unavailable.",
      ),
    });
    await setup(store);

    expect(await screen.findByText(/different account/)).toBeTruthy();
    expect(screen.queryByText("Bot alpha")).toBeNull();
  });

  it("keeps last-good rows visible and labels them stale after a refresh failure", async () => {
    const store = makeStore({
      error: signal(new Error("offline")),
      showingStaleLastGood: signal(true),
    });
    await setup(store);

    expect(await screen.findByText(/last attested holdings/)).toBeTruthy();
    expect(screen.getByText("Bot alpha")).toBeTruthy();
  });
});
