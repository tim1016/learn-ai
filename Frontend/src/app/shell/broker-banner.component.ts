import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { BrokerHealthService } from '../services/broker-health.service';

/**
 * Always-on global banner reflecting IBKR connection state.
 *
 * Renders in the shell above the router-outlet so every page sees it.
 * Three visual states (yellow paper / red live / grey disconnected)
 * driven by ``BrokerHealthService.bannerState`` — see the service
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
      <div
        class="broker-banner"
        [class.is-paper]="state.kind === 'paper'"
        [class.is-live]="state.kind === 'live'"
        [class.is-disconnected]="state.kind === 'disconnected'"
        [class.is-disabled]="state.kind === 'disabled'"
        role="status"
        [attr.aria-label]="state.aria"
      >
        <span class="broker-banner-icon" aria-hidden="true">{{ state.icon }}</span>
        <span class="broker-banner-text">{{ state.text }}</span>
        @if (state.kind === 'disconnected') {
          <button
            type="button"
            class="broker-banner-cta"
            (click)="connect()"
            [disabled]="action !== null"
            aria-label="Connect to IB Gateway"
          >
            {{ action === 'connect' ? 'Connecting…' : 'Connect' }}
          </button>
        }
        @if (state.kind === 'paper' || state.kind === 'live') {
          <button
            type="button"
            class="broker-banner-cta"
            (click)="disconnect()"
            [disabled]="action !== null"
            aria-label="Disconnect from IB Gateway"
          >
            {{ action === 'disconnect' ? 'Disconnecting…' : 'Disconnect' }}
          </button>
        }
      </div>
    }
  `,
})
export class BrokerBannerComponent {
  private readonly healthService = inject(BrokerHealthService);
  readonly lifecycleAction = this.healthService.lifecycleAction;

  connect(): Promise<void> {
    return this.healthService.connect();
  }

  disconnect(): Promise<void> {
    return this.healthService.disconnect();
  }

  readonly banner = computed(() => {
    const state = this.healthService.bannerState();
    if (state === null) return null;
    if (state === 'disabled-host-runner-active') {
      return {
        kind: 'disabled' as const,
        icon: 'ℹ',
        text: 'Broker connection disabled — paper-run is active. Visit /broker/paper-run for live status.',
        aria: 'IBKR broker connection disabled — host-venv runner owns IBKR for paper-run',
      };
    }
    const h = this.healthService.health();
    if (state === 'paper') {
      return {
        kind: 'paper' as const,
        icon: '🟡',
        text: `PAPER MODE — ${h?.account_id ?? 'unknown'} · IBKR connected`,
        aria: 'Connected to IBKR paper account',
      };
    }
    if (state === 'live') {
      return {
        kind: 'live' as const,
        icon: '⚠️',
        text: `LIVE MODE — ${h?.account_id ?? 'unknown'} · IBKR connected`,
        aria: 'Connected to IBKR LIVE account — real money at risk',
      };
    }
    return {
      kind: 'disconnected' as const,
      icon: '⛔',
      text: 'BROKER DISCONNECTED',
      aria: 'IBKR broker is disconnected',
    };
  });
}
