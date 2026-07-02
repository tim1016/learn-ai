import { ChangeDetectionStrategy, Component, computed, inject, signal } from '@angular/core';
import { HttpErrorResponse } from '@angular/common/http';
import { BrokerHealthService } from '../services/broker-health.service';
import { LiveRunsService } from '../services/live-runs.service';
import { ActiveBotSidebarNoticeService } from './active-bot-sidebar-notice.service';
import type { ActiveBotSidebarNotice } from './active-bot-sidebar-notice.service';

/**
 * Sidebar broker connection control.
 *
 * Driven by ``BrokerHealthService.bannerState`` — see the service
 * docstring for why the truth source is ``health.is_paper`` and never
 * the ``IBKR_MODE`` env var.
 */
@Component({
  selector: 'app-broker-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  styleUrl: './broker-banner.component.scss',
  template: `
    @if (activeBotNotice(); as notice) {
      <details
        class="host-runner-sidebar-notice"
        [class.is-binding-invalid]="notice.kind === 'live-binding-invalid'"
        data-testid="sidebar-host-runner-notice"
      >
        <summary>{{ notice.summary }}</summary>
        <div class="host-runner-sidebar-detail">
          <p>{{ notice.message }}</p>
          @if (notice.command) {
            <pre><code>{{ notice.command }}</code></pre>
          }
          @if (notice.action; as action) {
            <button
              type="button"
              class="host-runner-sidebar-action"
              data-testid="sidebar-host-runner-action"
              [disabled]="noticeActionInFlight() === notice.instanceId"
              (click)="invokeNoticeAction(notice)"
            >
              {{ noticeActionInFlight() === notice.instanceId ? action.busyLabel : action.label }}
            </button>
          }
          @if (noticeActionError(); as err) {
            <p class="host-runner-sidebar-error" role="alert">{{ err }}</p>
          }
        </div>
      </details>
    }

    @let state = banner();
    @let action = lifecycleAction();
    @if (state) {
      <section
        class="broker-banner"
        [class.is-paper]="state.kind === 'paper'"
        [class.is-live]="state.kind === 'live'"
        [class.is-degraded]="state.kind === 'degraded'"
        [class.is-disconnected]="state.kind === 'disconnected'"
        [class.is-disabled]="state.kind === 'disabled'"
        [attr.aria-label]="state.aria"
      >
        <div class="broker-banner-copy" role="status" [attr.aria-label]="state.aria">
          <span class="broker-banner-kicker">IBKR</span>
          <span class="broker-banner-title">
            <span class="broker-banner-dot" aria-hidden="true"></span>
            {{ state.title }}
          </span>
          <span class="broker-banner-detail">{{ state.detail }}</span>
        </div>
        @if (state.toggleLabel; as label) {
          <button
            type="button"
            class="broker-toggle"
            [class.is-on]="state.connected"
            (click)="toggleConnection()"
            [disabled]="action !== null"
            [attr.aria-pressed]="state.connected"
            [attr.aria-label]="state.toggleAria"
          >
            <span class="broker-toggle-track" aria-hidden="true">
              <span class="broker-toggle-thumb"></span>
            </span>
            <span class="broker-toggle-label">{{ toggleText(label, action) }}</span>
          </button>
        }
      </section>
    }
  `,
})
export class BrokerBannerComponent {
  private readonly healthService = inject(BrokerHealthService);
  private readonly liveRuns = inject(LiveRunsService);
  private readonly activeBotNoticeService = inject(ActiveBotSidebarNoticeService);
  readonly lifecycleAction = this.healthService.lifecycleAction;
  readonly activeBotNotice = this.activeBotNoticeService.activeNotice;
  readonly noticeActionInFlight = signal<string | null>(null);
  private readonly noticeActionErrorState = signal<{ instanceId: string; message: string } | null>(null);
  readonly noticeActionError = computed<string | null>(() => {
    const notice = this.activeBotNotice();
    const error = this.noticeActionErrorState();
    return notice !== null && error?.instanceId === notice.instanceId ? error.message : null;
  });

  toggleConnection(): Promise<void> {
    const state = this.banner();
    if (state === null || state.toggleLabel === null) return Promise.resolve();
    if (state.connected) return this.healthService.disconnect();
    return this.healthService.connect();
  }

  toggleText(label: 'Connect' | 'Disconnect', action: string | null): string {
    if (action === 'connect') return 'Connecting';
    if (action === 'disconnect') return 'Disconnecting';
    return label;
  }

  async invokeNoticeAction(notice: ActiveBotSidebarNotice): Promise<void> {
    const action = notice.action;
    if (action === null || this.noticeActionInFlight() !== null) return;
    this.noticeActionInFlight.set(notice.instanceId);
    this.noticeActionErrorState.set(null);
    try {
      await this.liveRuns.startHostRunner(action.runId, action.request);
    } catch (err) {
      this.noticeActionErrorState.set({
        instanceId: notice.instanceId,
        message: this.formatNoticeActionError(err),
      });
    } finally {
      this.noticeActionInFlight.set(null);
    }
  }

  readonly banner = computed(() => {
    const state = this.healthService.bannerState();
    if (state === null) return null;
    if (state === 'disabled-host-runner-active') {
      return {
        kind: 'disabled' as const,
        title: 'Host-owned',
        detail: 'Paper-run owns IBKR',
        aria: 'IBKR broker connection disabled — host-venv runner owns IBKR for paper-run',
        connected: false,
        toggleLabel: null,
        toggleAria: null,
      };
    }
    const h = this.healthService.health();
    if (state === 'paper') {
      return {
        kind: 'paper' as const,
        title: 'Paper connected',
        detail: h?.account_id ?? 'unknown account',
        aria: 'Connected to IBKR paper account',
        connected: true,
        toggleLabel: 'Disconnect' as const,
        toggleAria: 'Disconnect from IB Gateway',
      };
    }
    if (state === 'live') {
      return {
        kind: 'live' as const,
        title: 'Live connected',
        detail: h?.account_id ?? 'unknown account',
        aria: 'Connected to IBKR LIVE account — real money at risk',
        connected: true,
        toggleLabel: 'Disconnect' as const,
        toggleAria: 'Disconnect from IB Gateway',
      };
    }
    if (state === 'degraded') {
      const label = this.degradedLabel(h?.connection_state);
      return {
        kind: 'degraded' as const,
        title: 'Degraded',
        detail: label,
        aria: `IBKR broker degraded: ${label}`,
        connected: true,
        toggleLabel: 'Disconnect' as const,
        toggleAria: 'Disconnect from IB Gateway',
      };
    }
    return {
      kind: 'disconnected' as const,
      title: 'Disconnected',
      detail: 'IBKR offline',
      aria: 'IBKR broker is disconnected',
      connected: false,
      toggleLabel: 'Connect' as const,
      toggleAria: 'Connect to IB Gateway',
    };
  });

  private degradedLabel(state: string | undefined): string {
    switch (state) {
      case 'soft_lost':
        return 'feed lost, auto-recovery in progress';
      case 'reconnecting':
        return 'reconnecting';
      case 'recovering':
        return 'recovering subscriptions';
      case 'subscriptions_stale':
        return 'subscriptions stale';
      case 'degraded_data_farm':
        return 'data farm degraded';
      default:
        return 'not ready for orders';
    }
  }

  private formatNoticeActionError(err: unknown): string {
    if (err instanceof HttpErrorResponse) {
      const detail = err.error;
      if (typeof detail === 'string') return detail;
      if (detail && typeof detail === 'object' && 'detail' in detail && typeof detail.detail === 'string') {
        return detail.detail;
      }
      return err.message || 'Failed to start bot process.';
    }
    if (err instanceof Error) return err.message;
    return 'Failed to start bot process.';
  }
}
