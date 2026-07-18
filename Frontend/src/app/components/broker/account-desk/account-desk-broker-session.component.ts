import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  input,
  resource,
  signal,
} from "@angular/core";
import { RouterLink } from "@angular/router";

import type {
  DiagnosticReport,
  DiagnosticReportActive,
  IbkrConnectionHealth,
} from "../../../api/broker-models";
import type { AccountDeskLens } from "../../../api/operator-blocker.types";
import { ReceiptLabelPipe } from "../../../shared/pipes/receipt-label.pipe";
import { BrokerHealthService } from "../../../services/broker-health.service";
import { BrokerService } from "../../../services/broker.service";
import { AccountDeskBrokerCapabilityComponent } from "./account-desk-broker-capability.component";

interface BrokerConfigRow {
  readonly label: string;
  readonly value: string;
}

interface DiagnosticsState {
  readonly loading: boolean;
  readonly data: DiagnosticReport | null;
  readonly error: unknown | null;
}

const EMPTY_DIAGNOSTICS: DiagnosticsState = {
  loading: false,
  data: null,
  error: null,
};

/**
 * Account-scoped home for the connection state that used to live on Broker
 * Status. It never presents a broker session as evidence for a different
 * account route.
 */
@Component({
  selector: "app-account-desk-broker-session",
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    AccountDeskBrokerCapabilityComponent,
    ReceiptLabelPipe,
    RouterLink,
  ],
  templateUrl: "./account-desk-broker-session.component.html",
  styleUrl: "./account-desk-broker-session.component.scss",
})
export class AccountDeskBrokerSessionComponent {
  private readonly broker = inject(BrokerService);
  private readonly healthService = inject(BrokerHealthService);

  readonly accountId = input.required<string>();
  readonly lens = input.required<AccountDeskLens>();
  readonly health = this.healthService.health;
  readonly lifecycleAction = this.healthService.lifecycleAction;
  readonly lifecycleError = this.healthService.lifecycleError;
  private readonly diagnosticsState = signal<DiagnosticsState>(EMPTY_DIAGNOSTICS);
  private readonly capabilityProbeErrorState = signal<unknown | null>(null);

  readonly scope = computed<"loading" | "matched" | "mismatched" | "disconnected">(() => {
    const health = this.health();
    if (health === null) return "loading";
    if (!health.connected) return "disconnected";
    return health.account_id === this.accountId() ? "matched" : "mismatched";
  });
  readonly selectedHealth = computed<IbkrConnectionHealth | null>(() =>
    this.scope() === "matched" ? this.health() : null,
  );
  readonly diagnostics = this.diagnosticsState.asReadonly();
  readonly capabilityProbeError = this.capabilityProbeErrorState.asReadonly();
  readonly activeDiagnostics = computed<DiagnosticReportActive | null>(() => {
    const report = this.diagnostics().data;
    return report !== null && report.disabled === false ? report : null;
  });
  readonly capability = resource({
    params: () => (this.lens() === "operator" && this.scope() === "matched" ? this.accountId() : null),
    loader: async ({ params }) => {
      if (params === null) return [];
      const response = await this.broker.capability();
      return response.snapshots.filter((snapshot) => snapshot.account_id === params);
    },
  });
  readonly effectiveConfigRows = computed<readonly BrokerConfigRow[]>(() => {
    const health = this.selectedHealth();
    if (health === null) return [];
    const safety = health.safety_verdict;
    return [
      { label: "Mode", value: health.mode.toUpperCase() },
      { label: "Host", value: health.host },
      { label: "Port", value: String(health.port) },
      { label: "Data-plane client ID", value: String(health.client_id) },
      { label: "Connected account", value: health.account_id ?? "—" },
      {
        label: "Paper sentinel",
        value:
          safety?.final_verdict === "paper-only"
            ? "DU prefix matches paper"
            : (safety?.final_verdict ?? "—"),
      },
      {
        label: "Read-only API flag",
        value:
          safety?.readonly_flag === true
            ? "Read-only"
            : safety?.readonly_flag === false
              ? "Order-capable"
              : "—",
      },
    ];
  });
  readonly clientIdOverlapWarning = computed<string | null>(() => {
    const health = this.selectedHealth();
    if (
      health !== null &&
      (health.last_ibkr_code === 326 || health.last_ibkr_message?.toLowerCase().includes("client id"))
    ) {
      return "IBKR reports this client ID is already in use. Pick a non-overlapping data-plane ID or clear the stale session before reconnecting.";
    }
    const check = this.activeDiagnostics()?.checks.find(
      (candidate) => candidate.name.includes("client_id") || candidate.label.toLowerCase().includes("client id"),
    );
    return check?.fix ?? check?.detail ?? null;
  });

  async refresh(): Promise<void> {
    await this.healthService.refresh();
    this.capability.reload();
  }

  async connect(): Promise<void> {
    await this.healthService.connect();
    this.capability.reload();
  }

  async disconnect(): Promise<void> {
    await this.healthService.disconnect();
  }

  async reconnect(): Promise<void> {
    await this.healthService.reconnect();
    this.capability.reload();
  }

  async probeCapability(): Promise<void> {
    if (this.scope() !== "matched") return;
    this.capabilityProbeErrorState.set(null);
    try {
      await this.broker.probeCapability();
      this.capability.reload();
    } catch (error) {
      this.capabilityProbeErrorState.set(error);
    }
  }

  async runDiagnostics(): Promise<void> {
    this.diagnosticsState.set({ loading: true, data: null, error: null });
    try {
      const data = await this.broker.diagnose();
      this.diagnosticsState.set({ loading: false, data, error: null });
    } catch (error) {
      this.diagnosticsState.set({ loading: false, data: null, error });
    }
  }

  connectionLabel(health: IbkrConnectionHealth): string {
    if (health.condition !== null && health.condition !== undefined) return health.condition.title;
    if (health.connection_state === "connected") return "Data-plane broker session connected";
    return health.connection_state;
  }

  formatAge(value: number | null | undefined): string {
    if (value === null || value === undefined) return "—";
    const ageSeconds = Math.max(0, Math.floor((Date.now() - value) / 1000));
    if (ageSeconds < 60) return `${ageSeconds}s ago`;
    const ageMinutes = Math.floor(ageSeconds / 60);
    if (ageMinutes < 60) return `${ageMinutes}m ago`;
    return `${Math.floor(ageMinutes / 60)}h ago`;
  }
}
