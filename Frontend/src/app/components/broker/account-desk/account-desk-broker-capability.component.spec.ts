import { render, screen } from "@testing-library/angular";
import { describe, expect, it, vi } from "vitest";

import type { SessionDataCapability } from "../../../api/broker-models";
import { AccountDeskBrokerCapabilityComponent } from "./account-desk-broker-capability.component";

const capability: SessionDataCapability = {
  account_id: "DU1234567",
  account_mode: "paper",
  symbol: "SPY",
  con_id: 756733,
  probed_at_ms: 1_780_000_000_000,
  time_zone_id: "America/New_York",
  sessions: {
    RTH: { window_today_open_ms: null, window_today_close_ms: null, data: "live", tradeable: "yes", order_eligible_outside_rth: false, evidence_codes: [] },
    PRE: { window_today_open_ms: null, window_today_close_ms: null, data: "live", tradeable: "needs_enablement", order_eligible_outside_rth: false, evidence_codes: [10349] },
    POST: { window_today_open_ms: null, window_today_close_ms: null, data: "none", tradeable: "no", order_eligible_outside_rth: false, evidence_codes: [] },
    OVERNIGHT: { window_today_open_ms: null, window_today_close_ms: null, data: "none", tradeable: "needs_enablement", order_eligible_outside_rth: false, evidence_codes: [] },
  },
  raw_evidence: [],
};

describe("AccountDeskBrokerCapabilityComponent", () => {
  it("renders all session windows and their backend evidence for the selected account", async () => {
    await render(AccountDeskBrokerCapabilityComponent, {
      inputs: { snapshots: [capability], connected: true },
    });

    expect(await screen.findByText("Session capability")).toBeTruthy();
    expect(screen.queryByText("Operator evidence")).toBeNull();
    expect(screen.getAllByRole("heading", { name: "Session capability" })).toHaveLength(1);
    expect(screen.getByText("SPY")).toBeTruthy();
    expect(screen.getByText("live + tradeable")).toBeTruthy();
    expect(screen.getByText("Codes 10349")).toBeTruthy();
  });

  it("keeps probing unavailable while the selected account is disconnected", async () => {
    const probeRequested = vi.fn();
    await render(AccountDeskBrokerCapabilityComponent, {
      inputs: { snapshots: [], connected: false },
      on: { probeRequested },
    });

    expect(screen.getByRole("button", { name: "Probe capability" })).toHaveProperty("disabled", true);
    expect(probeRequested).not.toHaveBeenCalled();
  });
});
