import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { BrokerHealthService } from '../services/broker-health.service';

/**
 * Global IBKR market-data connection control.
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
        @if (state.toggleLabel) {
          <button
            type="button"
            class="broker-toggle"
            [class.is-on]="state.connected"
            [class.is-busy]="action !== null"
            (click)="toggleConnection()"
            [disabled]="action !== null"
            [attr.aria-pressed]="state.connected"
            [attr.aria-label]="state.toggleAria"
            [attr.aria-busy]="action !== null"
            [attr.title]="state.toggleAria"
          >
            <i
              class="pi"
              [class.pi-power-off]="action === null && state.connected"
              [class.pi-plug]="action === null && !state.connected"
              [class.pi-spinner]="action !== null"
              [class.pi-spin]="action !== null"
              aria-hidden="true"
            ></i>
          </button>
        }
        <div class="broker-banner-copy" role="status" [attr.aria-label]="state.aria">
          <span class="broker-banner-kicker">IBKR</span>
          <span class="broker-banner-title">
            <span class="broker-banner-dot" aria-hidden="true"></span>
            <span>Market data</span>
            <span class="broker-banner-state">{{ state.title }}</span>
          </span>
          <span class="broker-banner-detail" [attr.title]="state.detailTitle">
            {{ state.detail }}
          </span>
        </div>
      </section>
    }
  `,
})
export class BrokerBannerComponent {
  private readonly healthService = inject(BrokerHealthService);
  readonly lifecycleAction = this.healthService.lifecycleAction;

  toggleConnection(): Promise<void> {
    const state = this.banner();
    if (state === null || state.toggleLabel === null) return Promise.resolve();
    if (state.connected) return this.healthService.disconnect();
    return this.healthService.connect();
  }

  readonly banner = computed(() => {
    const state = this.healthService.bannerState();
    if (state === null) return null;
    const h = this.healthService.health();
    const condition = h?.condition ?? null;
    if (state === 'disabled') {
      return {
        kind: 'disabled' as const,
        title: condition?.title ?? 'Disabled',
        detail: condition?.summary ?? 'IBKR market data is disabled',
        detailTitle: condition?.summary ?? 'IBKR market data is disabled',
        aria: condition?.summary ?? 'IBKR market data is disabled',
        connected: false,
        toggleLabel: null,
        toggleAria: null,
      };
    }
    if (state === 'paper') {
      return {
        kind: 'paper' as const,
        title: condition?.title ?? 'Paper connected',
        detail: h?.account_id ?? 'unknown account',
        detailTitle: condition?.summary ?? h?.account_id ?? 'unknown account',
        aria: 'IBKR paper market data is connected',
        connected: true,
        toggleLabel: 'Disconnect' as const,
        toggleAria: 'Disconnect IBKR market data',
      };
    }
    if (state === 'live') {
      return {
        kind: 'live' as const,
        title: condition?.title ?? 'Live connected',
        detail: h?.account_id ?? 'unknown account',
        detailTitle: condition?.summary ?? h?.account_id ?? 'unknown account',
        aria: 'IBKR LIVE market data is connected — real money account',
        connected: true,
        toggleLabel: 'Disconnect' as const,
        toggleAria: 'Disconnect IBKR market data',
      };
    }
    if (state === 'degraded') {
      const label = this.degradedLabel(h?.connection_state);
      return {
        kind: 'degraded' as const,
        title: condition?.title ?? 'Degraded',
        detail: condition?.summary ?? label,
        detailTitle: condition?.summary ?? label,
        aria: condition?.summary ?? `IBKR market data degraded: ${label}`,
        connected: true,
        toggleLabel: 'Disconnect' as const,
        toggleAria: 'Disconnect IBKR market data',
      };
    }
    return {
      kind: 'disconnected' as const,
      title: condition?.title ?? 'Disconnected',
      detail: condition?.summary ?? 'IBKR offline',
      detailTitle: condition?.summary ?? 'IBKR offline',
      aria: condition?.summary ?? 'IBKR market data is disconnected',
      connected: false,
      toggleLabel: 'Connect' as const,
      toggleAria: 'Connect IBKR market data',
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
      case 'hard_down':
        return 'retrying automatically';
      case 'subscriptions_stale':
        return 'subscriptions stale';
      case 'degraded_data_farm':
        return 'data farm degraded';
      default:
        return 'not ready for orders';
    }
  }

}
