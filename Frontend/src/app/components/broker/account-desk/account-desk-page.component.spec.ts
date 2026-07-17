import { signal } from "@angular/core";
import { ActivatedRoute, Router, convertToParamMap } from "@angular/router";
import { fireEvent, render, screen, waitFor } from "@testing-library/angular";
import { BehaviorSubject } from "rxjs";
import { describe, expect, it, vi } from "vitest";

import type {
  AccountTriageResponse,
  AccountTriageVerdictState,
} from "../../../api/account-reconciliation.types";
import type {
  AccountRosterRow,
  AccountServiceStatusResponse,
} from "../../../api/account-directory.types";
import { BrokerService } from "../../../services/broker.service";
import { formatReceiptLabel } from "../../../shared/pipes/receipt-label.pipe";
import { formatTimestampDisplay } from "../../../shared/timestamp";
import { makeCleanAccountTriage } from "../testing/account-triage-fixtures";
import {
  makeAccountSummary,
  makeAccountTruth,
  makePositionsSnapshot,
} from "./account-desk-holdings.fixtures";
import { AccountDeskHoldingsStore } from "./account-desk-holdings-store.service";
import { AccountDeskEventsStore } from "./account-desk-events-store.service";
import { AccountDeskDirectoryStore } from "./account-desk-directory-store.service";
import { AccountDeskFleetStore } from "./account-desk-fleet-store.service";
import { AccountDeskGuidanceStore } from "./account-desk-guidance-store.service";
import { AccountDeskRecoveryStore } from "./account-desk-recovery-store.service";
import { AccountDeskSurfaceStore } from "./account-desk-surface-store.service";
import { AccountDeskPageComponent } from "./account-desk-page.component";

class FakeBrokerService {
  accountTriage =
    vi.fn<(accountId: string) => Promise<AccountTriageResponse>>();
  account = vi.fn().mockResolvedValue(makeAccountSummary());
  positions = vi.fn().mockResolvedValue(makePositionsSnapshot(undefined, []));
  accountTruth = vi.fn().mockResolvedValue(makeAccountTruth(undefined, []));
}

class StubEventSource {
  addEventListener = vi.fn();
  close = vi.fn();
}

vi.stubGlobal("EventSource", StubEventSource);

function makeEventsStore() {
  return {
    load: vi.fn().mockResolvedValue(undefined),
    traderRows: signal([]),
    traderLoading: signal(false),
    traderErrorMessage: signal<string | null>(null),
    traderHasLastGood: signal(false),
    traderShowingStaleLastGood: signal(false),
    operationRows: signal([]),
    operationsLoading: signal(false),
    operationsErrorMessage: signal<string | null>(null),
    operationsHasLastGood: signal(false),
    operationsShowingStaleLastGood: signal(false),
    nextBeforeSeq: signal<number | null>(null),
    operationKinds: signal<readonly string[]>([]),
    toggleOperationKind: vi.fn(),
    retry: vi.fn(),
    loadOlder: vi.fn(),
  };
}

function makeDirectoryStore(rows: readonly AccountRosterRow[] = []) {
  return {
    loadRoster: vi.fn().mockResolvedValue(undefined),
    loadServiceStatus: vi.fn().mockResolvedValue(undefined),
    retryRoster: vi.fn(),
    retryServiceStatus: vi.fn(),
    rosterRows: signal(rows),
    rosterLoading: signal(false),
    rosterErrorMessage: signal<string | null>(null),
    rosterHasLastGood: signal(rows.length > 0),
    rosterShowingStaleLastGood: signal(false),
    rosterEmpty: signal(rows.length === 0),
    statusAccountId: signal<string | null>(null),
    serviceStatus: signal<AccountServiceStatusResponse | null>(null),
    serviceStatusLoading: signal(false),
    serviceStatusErrorMessage: signal<string | null>(null),
    serviceStatusHasLastGood: signal(false),
    serviceStatusShowingStaleLastGood: signal(false),
  };
}

function makeGuidanceStore() {
  return {
    operatorAttentionCount: signal(0),
    blockersFor: vi.fn().mockReturnValue([]),
  };
}

function makeFleetStore() {
  return {
    load: vi.fn().mockResolvedValue(undefined),
    retry: vi.fn(),
    summary: signal(null),
    loading: signal(false),
    errorMessage: signal<string | null>(null),
    hasLastGood: signal(false),
    showingStaleLastGood: signal(false),
    lastGoodAtMs: signal<number | null>(null),
  };
}

function makeRecoveryStore() {
  return {
    load: vi.fn(),
    requestDeclaredMove: vi.fn(),
    requestAutomationChange: vi.fn(),
    requestJournalCure: vi.fn(),
    requestLegacyRetirement: vi.fn(),
    refreshLegacyCandidates: vi.fn(),
    setExposureOverrideReason: vi.fn(),
    cancelConfirmation: vi.fn(),
    confirm: vi.fn(),
    confirmation: signal(null),
    busy: signal(false),
    errorMessage: signal<string | null>(null),
    success: signal(null),
    legacyCandidates: signal([]),
    legacyLoading: signal(false),
    legacyErrorMessage: signal<string | null>(null),
  };
}

function triage(
  state: AccountTriageVerdictState = "CLEAN",
): AccountTriageResponse {
  const current = makeCleanAccountTriage({
    generatedAtMs: 1_780_000_002_000,
    affectedBots: [
      {
        strategy_instance_id: "bot-a",
        run_id: "run-a",
        bot_order_namespace: "learn-ai/bot-a",
        lifecycle_state: "ACTIVE",
      },
    ],
  });
  return {
    ...current,
    verdict: {
      state,
      headline: `${state} verdict`,
      detail: `${state} detail`,
      primary_move:
        state === "CLEAN"
          ? null
          : {
              label: "Open account desk",
              route: "/broker/accounts/DU1234567",
              fragment: "account-desk-recovery-controls",
            },
      operator_attention_count: state === "NEEDS_ATTENTION" ? 2 : 0,
    },
  };
}

async function setup(
  options: {
    response?: AccountTriageResponse;
    route$?: BehaviorSubject<ReturnType<typeof convertToParamMap>>;
    fragment$?: BehaviorSubject<string | null>;
    waitForVerdict?: boolean;
  } = {},
) {
  const broker = new FakeBrokerService();
  broker.accountTriage.mockResolvedValue(options.response ?? triage());
  const route$ =
    options.route$ ??
    new BehaviorSubject(convertToParamMap({ accountId: "DU1234567" }));
  const fragment$ =
    options.fragment$ ?? new BehaviorSubject<string | null>(null);
  const router = { navigate: vi.fn().mockResolvedValue(true) };
  const events = makeEventsStore();
  const fleet = makeFleetStore();
  const guidance = makeGuidanceStore();
  const recovery = makeRecoveryStore();
  const directory = makeDirectoryStore([
    accountRow("DU1234567"),
    accountRow("DU7654321"),
  ]);
  const view = await render(AccountDeskPageComponent, {
    providers: [
      AccountDeskHoldingsStore,
      AccountDeskSurfaceStore,
      { provide: AccountDeskEventsStore, useValue: events },
      { provide: AccountDeskDirectoryStore, useValue: directory },
      { provide: AccountDeskFleetStore, useValue: fleet },
      { provide: AccountDeskGuidanceStore, useValue: guidance },
      { provide: AccountDeskRecoveryStore, useValue: recovery },
      { provide: BrokerService, useValue: broker },
      {
        provide: ActivatedRoute,
        useValue: {
          paramMap: route$.asObservable(),
          fragment: fragment$.asObservable(),
        },
      },
      { provide: Router, useValue: router },
    ],
  });
  if (options.waitForVerdict !== false) {
    await screen.findByText((options.response ?? triage()).verdict.headline);
  }
  return {
    ...view,
    broker,
    directory,
    events,
    fleet,
    guidance,
    recovery,
    route$,
    fragment$,
    router,
  };
}

describe("AccountDeskPageComponent", () => {
  it.each(["FROZEN", "NOT_PROVEN", "NEEDS_ATTENTION", "CLEAN"] as const)(
    "renders the server-owned %s verdict without recomputing posture",
    async (state) => {
      await setup({ response: triage(state) });

      expect(screen.getByText(`${state} verdict`)).toBeTruthy();
      expect(screen.getByText(formatReceiptLabel(state))).toBeTruthy();
    },
  );

  it("defaults to the trader lens, keeps the verdict visible, and exposes pressed toggle state", async () => {
    await setup({ response: triage("NEEDS_ATTENTION") });

    const trader = screen.getByRole("button", { name: "Trader" });
    const operator = screen.getByRole("button", { name: "Operator" });
    await waitFor(() =>
      expect(trader.getAttribute("aria-pressed")).toBe("true"),
    );
    expect(operator.getAttribute("aria-pressed")).toBe("false");
    fireEvent.click(operator);
    await waitFor(() =>
      expect(operator.getAttribute("aria-pressed")).toBe("true"),
    );
    expect(screen.getByText("NEEDS_ATTENTION verdict")).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Resolve the account posture" }),
    ).toBeTruthy();
    expect(
      screen.getByRole("heading", { name: "Account recovery" }),
    ).toBeTruthy();
    expect(screen.getByText("Journal timeline")).toBeTruthy();
  });

  it("keeps operator actions and recovery ahead of the audit history", async () => {
    const { fixture } = await setup({ response: triage("NEEDS_ATTENTION") });
    fireEvent.click(screen.getByRole("button", { name: "Operator" }));

    const operatorWorkspace = (
      fixture.nativeElement as HTMLElement
    ).querySelector<HTMLElement>(".operator-workspace");
    const recovery = (
      fixture.nativeElement as HTMLElement
    ).querySelector<HTMLElement>("#account-desk-recovery-controls");
    const timeline = screen.getByRole("heading", {
      name: "Journal timeline",
    });

    expect(operatorWorkspace).toBeTruthy();
    expect(recovery).toBeTruthy();
    if (recovery === null)
      throw new Error("Expected account recovery controls to render.");
    expect(
      recovery.compareDocumentPosition(timeline) &
        Node.DOCUMENT_POSITION_FOLLOWING,
    ).toBeTruthy();
  });

  it("rekeys the route-scoped surface store when the account route changes", async () => {
    const route$ = new BehaviorSubject(
      convertToParamMap({ accountId: "DU1234567" }),
    );
    const { broker, directory, events, recovery } = await setup({ route$ });
    broker.accountTriage.mockResolvedValueOnce(
      makeCleanAccountTriage({ accountId: "DU7654321" }),
    );

    route$.next(convertToParamMap({ accountId: "DU7654321" }));
    await waitFor(() =>
      expect(broker.accountTriage).toHaveBeenCalledWith("DU7654321"),
    );
    expect(events.load).toHaveBeenCalledWith("DU7654321");
    expect(recovery.load).toHaveBeenCalledWith("DU7654321");
    expect(directory.loadServiceStatus).toHaveBeenCalledWith("DU7654321");
    expect(await screen.findByText("DU7654321")).toBeTruthy();
  });

  it("loads an explicitly empty account route parameter", async () => {
    const route$ = new BehaviorSubject(
      convertToParamMap({ accountId: "DU1234567" }),
    );
    const { broker, directory, events, fleet, recovery } = await setup({
      route$,
    });
    broker.accountTriage.mockResolvedValueOnce(
      makeCleanAccountTriage({ accountId: "" }),
    );

    route$.next(convertToParamMap({ accountId: "" }));

    await waitFor(() => expect(broker.accountTriage).toHaveBeenCalledWith(""));
    expect(events.load).toHaveBeenCalledWith("");
    expect(fleet.load).toHaveBeenCalledWith("");
    expect(recovery.load).toHaveBeenCalledWith("");
    expect(directory.loadServiceStatus).toHaveBeenCalledWith("");
  });

  it('fails closed instead of showing one account under another account route', async () => {
    const route$ = new BehaviorSubject(
      convertToParamMap({ accountId: 'DUM284968' }),
    );
    const mismatched = makeCleanAccountTriage({ accountId: 'DU1234567' });
    await setup({ response: mismatched, route$, waitForVerdict: false });

    expect(await screen.findByText('We could not load this account verdict.')).toBeTruthy();
    expect(screen.queryByText(mismatched.verdict.headline)).toBeNull();
    expect((screen.getByRole('combobox', { name: 'Account' }) as HTMLSelectElement).value).toBe('DUM284968');
  });

  it('keeps the route account selected after the roster gains that account', async () => {
    const route$ = new BehaviorSubject(
      convertToParamMap({ accountId: 'DUM284968' }),
    );
    const { directory } = await setup({
      response: makeCleanAccountTriage({ accountId: 'DUM284968' }),
      route$,
    });

    directory.rosterRows.set([
      accountRow('DU1234567'),
      accountRow('DUM284968'),
    ]);

    await waitFor(() =>
      expect((screen.getByRole('combobox', { name: 'Account' }) as HTMLSelectElement).value).toBe('DUM284968'),
    );
  });

  it("switches accounts without leaving the current lens", async () => {
    const route$ = new BehaviorSubject(
      convertToParamMap({ accountId: "DU1234567" }),
    );
    const { broker, router } = await setup({ route$ });
    broker.accountTriage.mockResolvedValueOnce(
      makeCleanAccountTriage({ accountId: "DU7654321" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Operator" }));

    fireEvent.change(screen.getByRole("combobox", { name: "Account" }), {
      target: { value: "DU7654321" },
    });
    expect(router.navigate).toHaveBeenCalledWith([
      "/broker/accounts",
      "DU7654321",
    ]);

    route$.next(convertToParamMap({ accountId: "DU7654321" }));
    await waitFor(() =>
      expect(broker.accountTriage).toHaveBeenCalledWith("DU7654321"),
    );
    expect(
      screen
        .getByRole("button", { name: "Operator" })
        .getAttribute("aria-pressed"),
    ).toBe("true");
  });

  it("consumes a legacy operations anchor once, selects the operator lens, and focuses its semantic target", async () => {
    const fragment$ = new BehaviorSubject<string | null>(
      "account-desk-recovery-controls",
    );
    const { fixture, router } = await setup({ fragment$ });

    const operator = screen.getByRole("button", { name: "Operator" });
    await waitFor(() =>
      expect(operator.getAttribute("aria-pressed")).toBe("true"),
    );
    const target = (
      fixture.nativeElement as HTMLElement
    ).querySelector<HTMLElement>("#account-desk-recovery-controls");
    await waitFor(() => expect(document.activeElement).toBe(target));
    expect(router.navigate).toHaveBeenCalledWith(
      [],
      expect.objectContaining({
        fragment: undefined,
        queryParamsHandling: "preserve",
        replaceUrl: true,
      }),
    );
  });

  it("uses the shared viewer-local timestamp display and preserves stale last-good data with retry", async () => {
    const { broker, fixture } = await setup({ response: triage() });
    expect(
      screen.getByText(
        formatTimestampDisplay(1_780_000_002_000, { mode: "local" }),
      ),
    ).toBeTruthy();

    broker.accountTriage.mockRejectedValueOnce(new Error("offline"));
    fixture.componentInstance.retry();
    await screen.findByText(/Showing last good account data/);
    expect(screen.getByText("CLEAN verdict")).toBeTruthy();
  });

  it("shows an explicit empty state and retries an initial fetch failure", async () => {
    const broker = new FakeBrokerService();
    broker.accountTriage
      .mockRejectedValueOnce(new Error("offline"))
      .mockResolvedValueOnce(makeCleanAccountTriage());
    const route$ = new BehaviorSubject(
      convertToParamMap({ accountId: "DU1234567" }),
    );
    const events = makeEventsStore();
    const directory = makeDirectoryStore();
    const fleet = makeFleetStore();
    const guidance = makeGuidanceStore();
    const recovery = makeRecoveryStore();
    await render(AccountDeskPageComponent, {
      providers: [
        AccountDeskHoldingsStore,
        AccountDeskSurfaceStore,
        { provide: AccountDeskEventsStore, useValue: events },
        { provide: AccountDeskDirectoryStore, useValue: directory },
        { provide: AccountDeskFleetStore, useValue: fleet },
        { provide: AccountDeskGuidanceStore, useValue: guidance },
        { provide: AccountDeskRecoveryStore, useValue: recovery },
        { provide: BrokerService, useValue: broker },
        {
          provide: ActivatedRoute,
          useValue: {
            paramMap: route$.asObservable(),
            fragment: new BehaviorSubject<string | null>(null).asObservable(),
          },
        },
        {
          provide: Router,
          useValue: { navigate: vi.fn().mockResolvedValue(true) },
        },
      ],
    });

    const retries = await screen.findAllByRole("button", { name: "Retry" });
    fireEvent.click(retries[0]);
    await waitFor(() =>
      expect(
        screen.getByText(
          "No open holdings are reported for this attested account.",
        ),
      ).toBeTruthy(),
    );
  });
});

function accountRow(accountId: string): AccountRosterRow {
  return {
    account_id: accountId,
    broker: "IBKR",
    effective_posture: "PAPER_EXECUTION",
    service: {
      attachment: "UNATTACHED",
      phase: null,
      generation: null,
      operating_state: "ATTENTION",
      headline: "Account service needs attention",
    },
    latest_verdict_summary: {
      state: "NOT_PROVEN",
      headline: "Verification is required.",
      generated_at_ms: 1_780_000_000_000,
    },
    last_verified_at_ms: null,
  };
}
