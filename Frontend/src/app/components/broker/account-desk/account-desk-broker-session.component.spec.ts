import { signal } from "@angular/core";
import { provideRouter } from "@angular/router";
import { fireEvent, render, screen, waitFor } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";

import type { DiagnosticReport, IbkrConnectionHealth } from "../../../api/broker-models";
import { BrokerHealthService } from "../../../services/broker-health.service";
import { BrokerService } from "../../../services/broker.service";
import { AccountDeskBrokerSessionComponent } from "./account-desk-broker-session.component";

class FakeBrokerHealthService {
  readonly health = signal<IbkrConnectionHealth | null>(connectedHealth());
  readonly lifecycleAction = signal<"connect" | "disconnect" | "reconnect" | null>(null);
  readonly lifecycleError = signal<unknown | null>(null);
  readonly refresh = vi.fn().mockResolvedValue(undefined);
  readonly connect = vi.fn().mockResolvedValue(undefined);
  readonly disconnect = vi.fn().mockResolvedValue(undefined);
  readonly reconnect = vi.fn().mockResolvedValue(undefined);
}

class FakeBrokerService {
  readonly capability = vi.fn().mockResolvedValue({ snapshots: [capabilitySnapshot()] });
  readonly probeCapability = vi.fn().mockResolvedValue({ snapshots: [] });
  readonly diagnose = vi.fn().mockResolvedValue(diagnosticReport());
}

async function setup(
  lens: "trader" | "operator",
  accountId = "DU1234567",
  health = new FakeBrokerHealthService(),
  broker = new FakeBrokerService(),
) {
  await render(AccountDeskBrokerSessionComponent, {
    inputs: { accountId, lens },
    providers: [
      { provide: BrokerHealthService, useValue: health },
      { provide: BrokerService, useValue: broker },
      provideRouter([]),
    ],
  });
  return { health, broker };
}

describe("AccountDeskBrokerSessionComponent", () => {
  it("gives traders the selected account's connection maintenance state without operator internals", async () => {
    const { broker } = await setup("trader");

    expect(await screen.findByText("IBKR connection")).toBeTruthy();
    expect(screen.getAllByRole("heading", { name: "IBKR connection" })).toHaveLength(1);
    expect(screen.queryByText("Selected-account broker session")).toBeNull();
    expect(screen.getByText("Data-plane broker session connected")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Reconnect" })).toBeTruthy();
    expect(screen.queryByText("Effective IBKR configuration")).toBeNull();
    expect(screen.queryByText("Session capability")).toBeNull();
    expect(broker.capability).not.toHaveBeenCalled();
  });

  it("fails closed when the gateway is attached to another account", async () => {
    await setup("operator", "DU1234567", new FakeBrokerHealthServiceWithAccount("DU7654321"));

    expect(await screen.findByText("Gateway attached to a different account")).toBeTruthy();
    expect(screen.getByText(/DU7654321, not DU1234567/)).toBeTruthy();
    expect(screen.queryByText("Effective IBKR configuration")).toBeNull();
    expect(screen.queryByText("Broker snapshot")).toBeNull();
  });

  it("preserves selected-account operator evidence and runs diagnostics on demand", async () => {
    const { broker } = await setup("operator");

    fireEvent.click(await screen.findByText("Connection detail and effective IBKR configuration"));
    expect(await screen.findByText("Effective IBKR configuration")).toBeTruthy();
    expect(screen.getByText("host.containers.internal")).toBeTruthy();
    expect(await screen.findByText("SPY")).toBeTruthy();
    expect(screen.queryByText("Connection guide")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Diagnose" }));
    await waitFor(() => expect(screen.getByText("Client ID uniqueness")).toBeTruthy());
    expect(broker.diagnose).toHaveBeenCalledOnce();
  });
});

class FakeBrokerHealthServiceWithAccount extends FakeBrokerHealthService {
  constructor(accountId: string) {
    super();
    this.health.set({ ...connectedHealth(), account_id: accountId });
  }
}

function connectedHealth(): IbkrConnectionHealth {
  return {
    mode: "paper",
    host: "host.containers.internal",
    port: 4002,
    client_id: 7,
    connected: true,
    disabled: false,
    reason: null,
    account_id: "DU1234567",
    is_paper: true,
    server_version: 178,
    fetched_at_ms: 1_780_000_000_000,
    safety_verdict: {
      configured_mode: "paper",
      readonly_flag: false,
      port_class: "paper_port",
      connected_account_prefix: "DU",
      final_verdict: "paper-only",
      failing_gates: [],
      unknown_gates: [],
    },
    connection_state: "connected",
    last_transition_ms: 1_780_000_000_000,
    connection_lost: false,
    connectivity_lost_count: 0,
    reconnect_attempt: null,
    condition: null,
    last_ibkr_code: null,
    last_ibkr_message: null,
    subscriptions_stale: false,
    data_farm_degraded: false,
    last_probe_ms: 1_780_000_000_000,
    last_probe_error: null,
    last_recovery_ms: null,
    recovery_error: null,
  };
}

function capabilitySnapshot() {
  const session = { window_today_open_ms: null, window_today_close_ms: null, data: "live" as const, tradeable: "yes" as const, order_eligible_outside_rth: false, evidence_codes: [2104] };
  return { symbol: "SPY", con_id: 756733, account_mode: "paper" as const, account_id: "DU1234567", probed_at_ms: 1_780_000_000_000, time_zone_id: "America/New_York", sessions: { RTH: session, PRE: session, POST: session, OVERNIGHT: session }, raw_evidence: [] };
}

function diagnosticReport(): DiagnosticReport {
  return { disabled: false, overall_status: "fail", fetched_at_ms: 1_780_000_000_000, checks: [{ name: "client_id_unique", label: "Client ID uniqueness", status: "fail", detail: "IBKR says client id is already in use by another API session.", fix: "Choose another IBKR_CLIENT_ID or stop the stale API session before reconnecting." }] };
}
