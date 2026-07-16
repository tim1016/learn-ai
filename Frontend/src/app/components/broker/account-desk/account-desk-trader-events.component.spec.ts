import { signal } from "@angular/core";
import { render, screen } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";

import { AccountDeskEventsStore } from "./account-desk-events-store.service";
import { AccountDeskTraderEventsComponent } from "./account-desk-trader-events.component";

function makeStore(overrides: Record<string, unknown> = {}) {
  return {
    traderRows: signal([
      {
        schema_version: 1 as const,
        event_id: "DU1234567:2",
        seq: 2,
        kind: "safety" as const,
        occurred_at_ms: 1_780_000_000_000,
        trader_narration: "The backend wrote this trader narration.",
        operator_detail: "Operator detail stays out of this feed.",
        evidence_refs: [],
      },
    ]),
    traderLoading: signal(false),
    traderErrorMessage: signal<string | null>(null),
    traderHasLastGood: signal(true),
    traderShowingStaleLastGood: signal(false),
    retry: vi.fn(),
    ...overrides,
  };
}

describe("AccountDeskTraderEventsComponent", () => {
  it("renders only backend narration with local timestamp display", async () => {
    await render(AccountDeskTraderEventsComponent, {
      providers: [{ provide: AccountDeskEventsStore, useValue: makeStore() }],
    });

    expect(await screen.findByText("Today at the desk")).toBeTruthy();
    expect(screen.getByRole("listitem").textContent).toContain(
      "The backend wrote this trader narration.",
    );
    expect(
      screen.queryByText("Operator detail stays out of this feed."),
    ).toBeNull();
    expect(
      document.querySelector('[data-timestamp-mode="local"]'),
    ).not.toBeNull();
  });

  it("renders an honest error with retry instead of an empty event feed", async () => {
    const store = makeStore({
      traderRows: signal([]),
      traderErrorMessage: signal("Account event history is unavailable."),
      traderHasLastGood: signal(false),
    });
    await render(AccountDeskTraderEventsComponent, {
      providers: [{ provide: AccountDeskEventsStore, useValue: store }],
    });

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Account event history is unavailable.",
    );
    expect(screen.queryByText(/No trader-facing/)).toBeNull();
  });
});
