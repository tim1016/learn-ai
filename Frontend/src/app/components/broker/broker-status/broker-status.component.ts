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

  trackPosition = (_: number, p: IbkrPosition): string =>
    `${p.account_id}:${p.con_id}`;

  trackCheck = (_: number, c: { name: string }): string => c.name;
}
