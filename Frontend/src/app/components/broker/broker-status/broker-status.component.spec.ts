import { signal } from '@angular/core';
import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type { DiagnosticReport } from '../../../api/broker-models';
import { BrokerHealthService } from '../../../services/broker-health.service';
import { BrokerService } from '../../../services/broker.service';
import { BrokerStatusComponent } from './broker-status.component';

class FakeBrokerHealthService {
  readonly health = signal(connectedHealth());
  readonly bannerState = signal('paper');
  readonly lifecycleAction = signal<'connect' | 'disconnect' | 'reconnect' | null>(null);
  readonly lifecycleError = signal<unknown>(null);
  refresh = vi.fn().mockResolvedValue(undefined);
  connect = vi.fn().mockResolvedValue(undefined);
  disconnect = vi.fn().mockResolvedValue(undefined);
  reconnect = vi.fn().mockResolvedValue(undefined);
}

class FakeBrokerService {
  capability = vi.fn().mockResolvedValue({ snapshots: [] });
  account = vi.fn().mockResolvedValue({
    account_id: 'DU1234567',
    is_paper: true,
    cash_balance: 100_000,
    net_liquidation: 100_000,
    buying_power: 200_000,
    excess_liquidity: 100_000,
    init_margin: 0,
    maint_margin: 0,
    day_pnl: 0,
    unrealized_pnl: 0,
    realized_pnl: 0,
    fetched_at_ms: 1_780_000_000_000,
  });
  positions = vi.fn().mockResolvedValue({
    account_id: 'DU1234567',
    is_paper: true,
    positions: [],
    fetched_at_ms: 1_780_000_000_000,
    used_cache_fallback: false,
  });
  diagnose = vi.fn().mockResolvedValue(diagnosticReport());
  probeCapability = vi.fn().mockResolvedValue({ snapshots: [] });
}

describe('BrokerStatusComponent', () => {
  it('renders the read-only effective IBKR configuration and setup guide link', async () => {
    await render(BrokerStatusComponent, {
      providers: [
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: BrokerService, useClass: FakeBrokerService },
        provideRouter([]),
      ],
    });

    expect(await screen.findByText('Effective IBKR configuration')).toBeTruthy();
    expect(screen.getAllByText('Mode').length).toBeGreaterThan(0);
    expect(screen.getAllByText('PAPER').length).toBeGreaterThan(0);
    expect(screen.getAllByText('host.containers.internal').length).toBeGreaterThan(0);
    expect(screen.getByText('Data-plane client ID')).toBeTruthy();
    expect(screen.getAllByText('7').length).toBeGreaterThan(0);
    expect(screen.getByText('Read-only API flag')).toBeTruthy();
    expect(screen.getByText('Order-capable')).toBeTruthy();
    expect(screen.getAllByRole('link', { name: /setup guide/i })[0].getAttribute('href')).toBe(
      '/docs/ibkr-setup-guide',
    );
  });

  it('warns when diagnostics report a client-ID overlap', async () => {
    await render(BrokerStatusComponent, {
      providers: [
        { provide: BrokerHealthService, useClass: FakeBrokerHealthService },
        { provide: BrokerService, useClass: FakeBrokerService },
        provideRouter([]),
      ],
    });

    fireEvent.click(await screen.findByRole('button', { name: /diagnose/i }));

    await waitFor(() => {
      expect(screen.getByText('Client ID overlap warning')).toBeTruthy();
    });
    expect(screen.getAllByText(/Choose another IBKR_CLIENT_ID/).length).toBeGreaterThan(0);
  });
});

function connectedHealth() {
  return {
    mode: 'paper',
    host: 'host.containers.internal',
    port: 4002,
    client_id: 7,
    connected: true,
    disabled: false,
    reason: null,
    account_id: 'DU1234567',
    is_paper: true,
    server_version: 178,
    fetched_at_ms: 1_780_000_000_000,
    safety_verdict: {
      configured_mode: 'paper',
      readonly_flag: false,
      port_class: 'paper_port',
      connected_account_prefix: 'DU',
      final_verdict: 'paper-only',
      failing_gates: [],
      unknown_gates: [],
    },
    connection_state: 'connected',
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

function diagnosticReport(): DiagnosticReport {
  return {
    disabled: false,
    overall_status: 'fail',
    fetched_at_ms: 1_780_000_000_000,
    checks: [
      {
        name: 'client_id_unique',
        label: 'Client ID uniqueness',
        status: 'fail',
        detail: 'IBKR says client id is already in use by another API session.',
        fix: 'Choose another IBKR_CLIENT_ID or stop the stale API session before reconnecting.',
      },
    ],
  };
}
