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
import type {
  DiagnosticReport,
  DiagnosticReportActive,
  IbkrAccountSummary,
  IbkrPosition,
  IbkrPositionsSnapshot,
} from '../../../api/broker-models';
import {
  fmtCurrency,
  fmtDateNy,
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
  imports: [PageHeaderComponent, PageGuideComponent, SectionErrorComponent, UpperCasePipe, RouterLink],
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

  /**
   * Which lifecycle action (connect/disconnect/reconnect) is in flight.
   * Disables all three buttons simultaneously so a frustrated operator
   * cannot stack actions on top of an outstanding connect.
   */
  readonly lifecycleAction = signal<'connect' | 'disconnect' | 'reconnect' | null>(null);
  readonly lifecycleError = signal<unknown | null>(null);

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

  /** Truthy iff the post-connect sentinel agrees with mode. */
  readonly sentinelOk = computed(() => {
    const h = this.health();
    if (h === null || !h.connected || h.account_id == null) return null;
    return h.mode === 'paper' ? h.is_paper === true : h.is_paper === false;
  });

  readonly fmtCurrency = fmtCurrency;
  readonly fmtSignedCurrency = fmtSignedCurrency;
  readonly fmtInteger = fmtInteger;
  readonly fmtDateNy = fmtDateNy;

  constructor() {
    void this.refresh();
  }

  async refresh(): Promise<void> {
    await this.healthService.refresh();
    if (!this.showAccountAndPositions()) {
      this.account.set({ ...EMPTY_CARD });
      this.positions.set({ ...EMPTY_CARD });
      return;
    }
    await Promise.all([this.loadAccount(), this.loadPositions()]);
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

  connect(): Promise<void> {
    return this.runLifecycleAction('connect', () => this.broker.connect());
  }

  disconnect(): Promise<void> {
    return this.runLifecycleAction('disconnect', () => this.broker.disconnect());
  }

  reconnect(): Promise<void> {
    return this.runLifecycleAction('reconnect', () => this.broker.reconnect());
  }

  private async runLifecycleAction(
    action: 'connect' | 'disconnect' | 'reconnect',
    call: () => Promise<unknown>,
  ): Promise<void> {
    if (this.lifecycleAction() !== null) return;
    this.lifecycleAction.set(action);
    this.lifecycleError.set(null);
    try {
      await call();
    } catch (err) {
      this.lifecycleError.set(err);
    } finally {
      this.lifecycleAction.set(null);
      // Refresh health + dependent cards so the banner and Status page
      // reflect the post-action state immediately, not five seconds later.
      await this.refresh();
    }
  }

  trackPosition = (_: number, p: IbkrPosition): string =>
    `${p.account_id}:${p.con_id}`;

  trackCheck = (_: number, c: { name: string }): string => c.name;
}
