import { signal } from "@angular/core";
import { Router } from "@angular/router";
import { fireEvent, render, screen } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";

import { AccountDeskEventsStore } from "./account-desk-events-store.service";
import { AccountDeskGuidanceStore } from "./account-desk-guidance-store.service";
import { AccountDeskOperatorEventsComponent } from "./account-desk-operator-events.component";

function makeStore(overrides: Record<string, unknown> = {}) {
  return {
    operationRows: signal([
      {
        schema_version: 1 as const,
        event_id: "DU1234567:5",
        seq: 5,
        kind: "reconciliation" as const,
        occurred_at_ms: 1_780_000_000_000,
        trader_narration: null,
        operator_detail:
          "Account reconciliation receipt recorded in the journal.",
        evidence_refs: [
          { source: "account_event_journal", ref: "DU1234567:5", detail: null },
        ],
      },
    ]),
    operationsLoading: signal(false),
    operationsErrorMessage: signal<string | null>(null),
    operationsHasLastGood: signal(true),
    operationsShowingStaleLastGood: signal(false),
    nextBeforeSeq: signal<number | null>(4),
    operationKinds: signal<readonly string[]>([]),
    toggleOperationKind: vi.fn(),
    retryOperations: vi.fn(),
    loadOlder: vi.fn(),
    ...overrides,
  };
}

describe("AccountDeskOperatorEventsComponent", () => {
  it("renders safety/configuration evidence with local instants, filters, and load older", async () => {
    const store = makeStore();
    const view = await render(AccountDeskOperatorEventsComponent, {
      providers: [
        { provide: AccountDeskEventsStore, useValue: store },
        {
          provide: AccountDeskGuidanceStore,
          useValue: { blockersFor: vi.fn().mockReturnValue([]) },
        },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect(await screen.findByText("Safety and configuration evidence")).toBeTruthy();
    expect(
      screen.getByText(
        "Account reconciliation receipt recorded in the journal.",
      ),
    ).toBeTruthy();
    expect(document.querySelector('[data-kind="reconciliation"]')).not.toBeNull();
    expect(screen.getByText("DU1234567:5")).toBeTruthy();
    expect(screen.getByRole("list", { name: "Safety and configuration events" })).toBeTruthy();
    expect(document.querySelectorAll('[aria-label="Safety and configuration events"] > [role="listitem"]')).toHaveLength(1);
    expect(
      document.querySelector('[data-timestamp-mode="local"]'),
    ).not.toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Safety" }));
    fireEvent.click(screen.getByRole("button", { name: "Load older" }));
    expect(store.toggleOperationKind).toHaveBeenCalledWith("safety");
    expect(store.loadOlder).toHaveBeenCalledOnce();

    const firstRow = view.fixture.componentInstance.timelineRows()[0];
    store.operationRows.set(store.operationRows().map((event) => ({ ...event })));
    expect(view.fixture.componentInstance.timelineRows()[0]).toBe(firstRow);
  });

  it("does not render manual-order receipts in the safety/configuration surface", async () => {
    const store = makeStore({
      operationRows: signal([
        {
          schema_version: 1 as const,
          event_id: "DU1234567:6",
          seq: 6,
          kind: "activity" as const,
          occurred_at_ms: 1_780_000_000_100,
          trader_narration: "Your paper order was received by the broker.",
          operator_detail: "Account Clerk recorded a durable IBKR acknowledgement for a manual paper order.",
          evidence_refs: [],
          operator_order_receipt: {
            broker: "ibkr" as const,
            order_id: 42,
            perm_id: 9001,
            order_ref: "manual/operator/v1:opaque-1",
            symbol: "SPY",
            action: "BUY" as const,
            quantity: 3,
            order_type: "LMT" as const,
            limit_price: 593.25,
            status: "Submitted",
            acknowledged_at_ms: 1_780_000_000_100,
          },
        },
      ]),
      nextBeforeSeq: signal(null),
    });
    await render(AccountDeskOperatorEventsComponent, {
      providers: [
        { provide: AccountDeskEventsStore, useValue: store },
        { provide: AccountDeskGuidanceStore, useValue: { blockersFor: vi.fn().mockReturnValue([]) } },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect(document.querySelector('[aria-label="Manual-order receipt"]')).toBeNull();
  });

  it("renders an honest operations error rather than empty history", async () => {
    const store = makeStore({
      operationRows: signal([]),
      operationsErrorMessage: signal("Account event history is unavailable."),
      operationsHasLastGood: signal(false),
      nextBeforeSeq: signal(null),
    });
    await render(AccountDeskOperatorEventsComponent, {
      providers: [
        { provide: AccountDeskEventsStore, useValue: store },
        {
          provide: AccountDeskGuidanceStore,
          useValue: { blockersFor: vi.fn().mockReturnValue([]) },
        },
        { provide: Router, useValue: { navigate: vi.fn() } },
      ],
    });

    expect((await screen.findByRole("alert")).textContent).toContain(
      "Account event history is unavailable.",
    );
    expect(screen.queryByText(/No account journal/)).toBeNull();
  });
});
