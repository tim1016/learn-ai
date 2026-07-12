import {
  ChangeDetectionStrategy,
  Component,
  computed,
  inject,
  signal,
} from '@angular/core';
import { UpperCasePipe } from '@angular/common';
import { RouterLink } from '@angular/router';
import { PageHeaderComponent } from '../../../shared/page-header/page-header.component';
import { PageGuideComponent } from '../../../shared/page-guide/page-guide.component';
import { SectionErrorComponent } from '../../../shared/errors/section-error.component';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { BrokerCapabilityPanelComponent } from './broker-capability-panel.component';
import type {
  BrokerCapabilityResponse,
  DiagnosticReport,
  DiagnosticReportActive,
  IbkrAccountSummary,
  IbkrPosition,
  IbkrPositionsSnapshot,
} from '../../../api/broker-models';
import {
  fmtBrokerExpiryDate,
  fmtCurrency,
  fmtInteger,
  fmtSignedCurrency,
} from '../format';

interface AsyncCard<T> {
  data: T | null;
  loading: boolean;
  error: unknown;
}

const EMPTY_CARD: AsyncCard<never> = { data: null, loading: false, error: null };

/**
 * /broker — Phase 1 Status page.
 *
 * Three cards: Connection, Account Snapshot, Positions. The Connection
 * card reads from the singleton ``BrokerHealthService`` (poll-driven);
 * the other two are fetched on mount and on manual refresh. Hides the
 * account / positions cards when ``health.connected === false`` so we
 * don't hammer endpoints that will return 503.
 */
@Component({
  selector: 'app-broker-status',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    BrokerCapabilityPanelComponent,
    PageHeaderComponent,
    PageGuideComponent,
    SectionErrorComponent,
    UpperCasePipe,
    RouterLink,
  ],
  styleUrl: './broker-status.component.scss',
  templateUrl: './broker-status.component.html',
})
export class BrokerStatusComponent {
  private readonly broker = inject(BrokerService);
  private readonly healthService = inject(BrokerHealthService);

  readonly health = this.healthService.health;
  readonly bannerState = this.healthService.bannerState;

  readonly account = signal<AsyncCard<IbkrAccountSummary>>({ ...EMPTY_CARD });
  readonly positions = signal<AsyncCard<IbkrPositionsSnapshot>>({ ...EMPTY_CARD });
  readonly diagnostics = signal<AsyncCard<DiagnosticReport>>({ ...EMPTY_CARD });
  readonly capability = signal<AsyncCard<BrokerCapabilityResponse>>({ ...EMPTY_CARD });

  /**
   * The lifecycle action signals live on ``BrokerHealthService`` so the
   * global banner and this page share one in-flight lock — clicking
   * Connect in the banner disables Connect here too, matching the
   * server-side asyncio lock on /api/broker/{connect,disconnect,reconnect}.
   */
  readonly lifecycleAction = this.healthService.lifecycleAction;
  readonly lifecycleError = this.healthService.lifecycleError;
  /**
   * The most recent lifecycle attempt. ``retryLastAction()`` replays
   * exactly this — retrying a failed Disconnect with Reconnect (the
   * naive bind) would flip the session state instead of finishing the
   * disconnect. Page-local because retry intent doesn't belong in the
   * shared service.
   */
  private readonly lastLifecycleAction = signal<'connect' | 'disconnect' | 'reconnect' | null>(null);

  /** Hide lifecycle controls when the broker subsystem is disabled. */
  readonly lifecycleControlsVisible = computed(() => {
    const h = this.health();
    return h !== null && h.disabled !== true;
  });

  readonly activeDiagReport = computed<DiagnosticReportActive | null>(() => {
    const d = this.diagnostics().data;
    return d != null && d.disabled === false ? d : null;
  });

  /**
   * Visible only when we know the broker is connected. The auth banner
   * already explains the disconnected case; the Status page reduces to
   * the connection card alone.
   */
  readonly showAccountAndPositions = computed(() => {
    const h = this.health();
    return h !== null && h.connected;
  });

  /** VCR-0018-A — Truthy iff the structured 4-layer broker safety verdict
   * (Phase 7A) agrees that this run is paper-only. Falls back to the
   * legacy 2-layer ``mode + is_paper`` check when the server hasn't yet
   * shipped the ``safety_verdict`` block (older endpoint, or a pre-Phase-7A
   * broker.py running in some legacy env). ``null`` while disconnected
   * or pre-first-response so the pill renders the loading state. */
  readonly sentinelOk = computed(() => {
    const h = this.health();
    if (h === null || !h.connected || h.account_id == null) return null;
    if (h.safety_verdict != null) {
      return h.safety_verdict.final_verdict === 'paper-only';
    }
    return h.mode === 'paper' ? h.is_paper === true : h.is_paper === false;
  });

  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedCurrency = fmtSignedCurrency;
  readonly fmtInteger = fmtInteger;
  readonly fmtBrokerExpiryDate = fmtBrokerExpiryDate;

  readonly fmtAge = (ms: number | null | undefined): string => {
    if (ms == null) return '—';
    const ageSeconds = Math.max(0, Math.floor((Date.now() - ms) / 1000));
    if (ageSeconds < 60) return `${ageSeconds}s ago`;
    const ageMinutes = Math.floor(ageSeconds / 60);
    if (ageMinutes < 60) return `${ageMinutes}m ago`;
    return `${Math.floor(ageMinutes / 60)}h ago`;
  };

  constructor() {
    void this.refresh();
  }

  async refresh(): Promise<void> {
    await this.healthService.refresh();
    await this.loadCapability();
    if (!this.showAccountAndPositions()) {
      this.account.set({ ...EMPTY_CARD });
      this.positions.set({ ...EMPTY_CARD });
      return;
    }
    await Promise.all([this.loadAccount(), this.loadPositions()]);
  }

  async loadCapability(): Promise<void> {
    this.capability.set({ data: this.capability().data, loading: true, error: null });
    try {
      const data = await this.broker.capability();
      this.capability.set({ data, loading: false, error: null });
    } catch (err) {
      this.capability.set({ data: this.capability().data, loading: false, error: err });
    }
  }

  async probeCapability(): Promise<void> {
    this.capability.set({ data: this.capability().data, loading: true, error: null });
    try {
      const data = await this.broker.probeCapability();
      this.capability.set({ data, loading: false, error: null });
    } catch (err) {
      this.capability.set({ data: this.capability().data, loading: false, error: err });
    }
  }

  async loadAccount(): Promise<void> {
    this.account.set({ data: null, loading: true, error: null });
    try {
      const data = await this.broker.account();
      this.account.set({ data, loading: false, error: null });
    } catch (err) {
      this.account.set({ data: null, loading: false, error: err });
    }
  }

  async loadPositions(): Promise<void> {
    this.positions.set({ data: null, loading: true, error: null });
    try {
      const data = await this.broker.positions();
      this.positions.set({ data, loading: false, error: null });
    } catch (err) {
      this.positions.set({ data: null, loading: false, error: err });
    }
  }

  async runDiagnostics(): Promise<void> {
    this.diagnostics.set({ data: null, loading: true, error: null });
    try {
      const data = await this.broker.diagnose();
      this.diagnostics.set({ data, loading: false, error: null });
    } catch (err) {
      this.diagnostics.set({ data: null, loading: false, error: err });
    }
  }

  async connect(): Promise<void> {
    this.lastLifecycleAction.set('connect');
    await this.healthService.connect();
    await this.refreshDependentCards();
  }

  async disconnect(): Promise<void> {
    this.lastLifecycleAction.set('disconnect');
    await this.healthService.disconnect();
    await this.refreshDependentCards();
  }

  async reconnect(): Promise<void> {
    this.lastLifecycleAction.set('reconnect');
    await this.healthService.reconnect();
    await this.refreshDependentCards();
  }

  /**
   * Replay the most recent lifecycle attempt — wired to the inline
   * error component's retry button. Defaults to reconnect when no
   * attempt is on record (e.g. cold load with no failures).
   */
  retryLastAction(): Promise<void> {
    switch (this.lastLifecycleAction()) {
      case 'connect':
        return this.connect();
      case 'disconnect':
        return this.disconnect();
      default:
        return this.reconnect();
    }
  }

  /**
   * Refresh the account + positions cards after a lifecycle action.
   * Health itself is refreshed inside ``BrokerHealthService.runLifecycleAction``;
   * here we only need the cards that depend on the post-action state.
   */
  private async refreshDependentCards(): Promise<void> {
    if (!this.showAccountAndPositions()) {
      this.account.set({ ...EMPTY_CARD });
      this.positions.set({ ...EMPTY_CARD });
      return;
    }
    await Promise.all([this.loadAccount(), this.loadPositions()]);
  }

  trackPosition = (_: number, p: IbkrPosition): string =>
    `${p.account_id}:${p.con_id}`;

  trackCheck = (_: number, c: { name: string }): string => c.name;
}
