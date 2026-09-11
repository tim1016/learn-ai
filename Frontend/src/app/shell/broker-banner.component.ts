import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { BrokerHealthService } from '../services/broker-health.service';

/**
 * Compact global IB Gateway connection control.
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
        class="broker-gateway"
        [class.is-paper]="state.kind === 'paper'"
        [class.is-live]="state.kind === 'live'"
        [class.is-degraded]="state.kind === 'degraded'"
        [class.is-disconnected]="state.kind === 'disconnected'"
        [class.is-disabled]="state.kind === 'disabled'"
        [attr.aria-label]="state.aria"
      >
        @if (state.toggleAria) {
          <button
            type="button"
            class="gateway-control"
            (click)="toggleConnection()"
            [disabled]="action !== null"
            [attr.aria-label]="state.toggleAria"
            [attr.aria-busy]="action !== null"
            [attr.title]="state.aria"
          >
            @if (action !== null) {
              <i class="pi pi-spinner pi-spin" aria-hidden="true"></i>
            } @else {
              <span class="gateway-dot" aria-hidden="true"></span>
            }
            <span>IB</span>
          </button>
        } @else {
          <span class="gateway-control gateway-control--static" role="status" [attr.title]="state.aria">
            <span class="gateway-dot" aria-hidden="true"></span>
            <span>IB</span>
          </span>
        }
      </section>
    }
  `,
})
export class BrokerBannerComponent {
  private readonly healthService = inject(BrokerHealthService);
  readonly lifecycleAction = this.healthService.lifecycleAction;

  toggleConnection(): Promise<void> {
    const state = this.banner();
    if (state === null || state.toggleAria === null) return Promise.resolve();
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
        aria: condition?.summary ?? 'IBKR market data is disabled',
        connected: false,
        toggleAria: null,
      };
    }
    if (state === 'paper') {
      return {
        kind: 'paper' as const,
        aria: 'IBKR paper market data is connected',
        connected: true,
        toggleAria: 'Disconnect IBKR market data',
      };
    }
    if (state === 'live') {
      return {
        kind: 'live' as const,
        aria: 'IBKR LIVE market data is connected — real money account',
        connected: true,
        toggleAria: 'Disconnect IBKR market data',
      };
    }
    if (state === 'degraded') {
      const label = this.degradedLabel(h?.connection_state);
      return {
        kind: 'degraded' as const,
        aria: condition?.summary ?? `IBKR market data degraded: ${label}`,
        connected: true,
        toggleAria: 'Disconnect IBKR market data',
      };
    }
    return {
      kind: 'disconnected' as const,
      aria: condition?.summary ?? 'IBKR market data is disconnected',
      connected: false,
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
