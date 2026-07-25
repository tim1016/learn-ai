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
        occurred_at_ms: 1_780_000_000_000,
        outcome: "The backend wrote this trader narration.",
      },
    ]),
    traderLoading: signal(false),
    traderErrorMessage: signal<string | null>(null),
    traderHasLastGood: signal(true),
    traderShowingStaleLastGood: signal(false),
    retryTrader: vi.fn(),
    ...overrides,
  };
}

describe("AccountDeskTraderEventsComponent", () => {
  it("renders only backend-authored outcome copy and local timestamp display", async () => {
    await render(AccountDeskTraderEventsComponent, {
      providers: [{ provide: AccountDeskEventsStore, useValue: makeStore() }],
    });

    expect(await screen.findByText("Today at the desk")).toBeTruthy();
    expect(screen.getByText("The backend wrote this trader narration.")).toBeTruthy();
    expect(document.querySelector('.trader-event-marker')).not.toBeNull();
    expect(
      document.querySelector('[data-timestamp-mode="local"]'),
    ).not.toBeNull();
    expect(screen.getByRole("list", { name: "Today at the desk activity" })).toBeTruthy();
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
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

  it('shows the most recent activity first and keeps older journal entries collapsed', async () => {
    const rows = Array.from({ length: 7 }, (_, index) => ({
      schema_version: 1 as const,
      event_id: `DU1234567:${index + 1}`,
      seq: index + 1,
      occurred_at_ms: 1_780_000_000_000 + index,
      outcome: `Account update ${index + 1}.`,
    }));
    await render(AccountDeskTraderEventsComponent, {
      providers: [{ provide: AccountDeskEventsStore, useValue: makeStore({ traderRows: signal(rows) }) }],
    });

    expect(screen.getByText('Showing the latest 5 of 7 updates.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Show 2 older updates' })).toBeTruthy();
  });
});
