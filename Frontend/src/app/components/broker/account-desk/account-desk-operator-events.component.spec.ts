import { signal } from "@angular/core";
import { Router } from "@angular/router";
import { fireEvent, render, screen } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";

import type { AccountEventRow } from "../../../api/account-events.types";
import { AccountDeskEventsStore } from "./account-desk-events-store.service";
import { AccountDeskGuidanceStore } from "./account-desk-guidance-store.service";
import { AccountDeskOperatorEventsComponent } from "./account-desk-operator-events.component";

function makeStore(overrides: Record<string, unknown> = {}) {
  return {
    operationRows: signal<AccountEventRow[]>([
      {
        schema_version: 1 as const,
        event_id: "DU1234567:5",
        provenance: "producer_operational_log" as const,
        seq: 5,
        kind: "reconciliation" as const,
        occurred_at_ms: 1_780_000_000_000,
        event_at_ms: null,
        arrived_at_ms: 1_780_000_000_000,
        recorded_at_ms: 1_780_000_000_001,
        producer: "data_plane",
        producer_boot_id: "test-boot",
        producer_seq: 5,
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
  it("renders account operations evidence with local instants, filters, and load older", async () => {
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

    expect(await screen.findByText("Account operations evidence")).toBeTruthy();
    expect(
      screen.getByText(
        "Account reconciliation receipt recorded in the journal.",
      ),
    ).toBeTruthy();
    expect(document.querySelector('[data-kind="reconciliation"]')).not.toBeNull();
    expect(screen.getByText("DU1234567:5")).toBeTruthy();
    expect(screen.getByRole("list", { name: "Account operations events" })).toBeTruthy();
    expect(document.querySelectorAll('[aria-label="Account operations events"] > [role="listitem"]')).toHaveLength(1);
    expect(
      document.querySelector('[data-timestamp-mode="local"]'),
    ).not.toBeNull();
    expect(screen.getByText("History source and clocks")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Safety" }));
    fireEvent.click(screen.getByRole("button", { name: "Load older" }));
    expect(store.toggleOperationKind).toHaveBeenCalledWith("safety");
    expect(store.loadOlder).toHaveBeenCalledOnce();

  });

  it("renders fresh details when a head refresh replaces an existing event", async () => {
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

    store.operationRows.update((rows) =>
      rows.map((event) => ({
        ...event,
        provenance: "clerk_journal" as const,
        producer: "clerk_journal",
        producer_boot_id: "recovered-clerk-boot",
        producer_seq: 8,
        operator_detail: "Reconciled event details replaced the prior timeline row.",
        evidence_refs: [
          { source: "account_clerk_journal", ref: "DU1234567:8", detail: "fresh evidence" },
        ],
      })),
    );
    view.fixture.detectChanges();
    await view.fixture.whenStable();

    expect(
      screen.getByText("Reconciled event details replaced the prior timeline row."),
    ).toBeTruthy();
    expect(screen.queryByText("Account reconciliation receipt recorded in the journal.")).toBeNull();
    expect(screen.getByText("Account Clerk Journal:")).toBeTruthy();
    expect(screen.getAllByText("Clerk Journal")).toHaveLength(2);
    expect(screen.getByText("recovered-clerk-boot")).toBeTruthy();
    expect(screen.getByText("DU1234567:8")).toBeTruthy();
  });

  it("does not render manual-order receipts in the safety/configuration surface", async () => {
    const store = makeStore({
      operationRows: signal([
        {
          schema_version: 1 as const,
          event_id: "DU1234567:6",
          provenance: "clerk_journal" as const,
          seq: 6,
          kind: "activity" as const,
          occurred_at_ms: 1_780_000_000_100,
          event_at_ms: 1_780_000_000_100,
          arrived_at_ms: null,
          recorded_at_ms: 1_780_000_000_101,
          producer: "clerk_journal",
          producer_boot_id: "canonical",
          producer_seq: 6,
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
