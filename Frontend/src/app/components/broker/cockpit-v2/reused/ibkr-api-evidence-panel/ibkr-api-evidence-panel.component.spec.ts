import { provideZonelessChangeDetection } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  DataPlaneHealth,
  DiagnosticReport,
  IbkrApiEvidenceEvent,
  IbkrConnectionHealth,
} from '../../../../../api/broker-models';
import { BrokerService } from '../../../../../services/broker.service';
import { IbkrApiEvidencePanelComponent } from './ibkr-api-evidence-panel.component';

class FakeEventSource {
  static instances: FakeEventSource[] = [];
  readonly listeners = new Map<string, EventListenerOrEventListenerObject[]>();
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: EventListenerOrEventListenerObject): void {
    const next = this.listeners.get(type) ?? [];
    next.push(listener);
    this.listeners.set(type, next);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, payload: unknown): void {
    const event = { data: JSON.stringify(payload) } as MessageEvent<string>;
    for (const listener of this.listeners.get(type) ?? []) {
      if (typeof listener === 'function') listener(event);
      else listener.handleEvent(event);
    }
  }
}

function connectionHealth(): IbkrConnectionHealth {
  return {
    mode: 'paper',
    host: '127.0.0.1',
    port: 4002,
    client_id: 12,
    connected: true,
    disabled: false,
    account_id: 'DU1234567',
    is_paper: true,
    server_version: 178,
    fetched_at_ms: 1_780_000_000_000,
    connection_state: 'connected',
    last_transition_ms: 1_780_000_000_000,
    reason: null,
    safety_verdict: {
      configured_mode: 'paper',
      readonly_flag: false,
      port_class: 'paper_port',
      connected_account_prefix: 'DU',
      final_verdict: 'paper-only',
      failing_gates: [],
      unknown_gates: [],
    },
  };
}

function dataPlaneHealth(): DataPlaneHealth {
  return {
    service: 'polygon-data-service',
    code_revision: '8398d285978a94d9714490e002962e365e9cd505',
    process_start_ms: 1_780_000_100_000,
    fetched_at_ms: 1_780_000_200_000,
    reload: 'watchfiles-polling',
  };
}

function diagnose(): DiagnosticReport {
  return {
    disabled: false,
    overall_status: 'warn',
    fetched_at_ms: 1_780_000_200_000,
    checks: [
      {
        name: 'gateway',
        label: 'IB Gateway',
        status: 'warn',
        detail: 'Connected with degraded evidence capture',
        fix: 'Restart data plane if code revision is stale.',
      },
    ],
  };
}

function evidence(seq: number, serializerError?: string): IbkrApiEvidenceEvent {
  return {
    seq,
    ts_ms: 1_780_000_300_000 + seq,
    source: 'broker.positions',
    account_id: 'DU1234567',
    symbol: 'SPY',
    strategy_instance_id: 'JUNE-25',
    request: { call: 'reqPositionsAsync', params: {} },
    response: {
      callback: 'position',
      fields: serializerError
        ? {
            object_0: {
              object_type: 'OpaqueIbkrObject',
              fields: { serializer_error: serializerError },
            },
          }
        : { row_count: 1 },
      serializer_warnings: serializerError
        ? [{ object_type: 'OpaqueIbkrObject', serializer_error: serializerError }]
        : [],
    },
    error: null,
  };
}

function deferred<T>(): {
  promise: Promise<T>;
  resolve: (value: T) => void;
  reject: (reason?: unknown) => void;
} {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

describe('IbkrApiEvidencePanelComponent', () => {
  let broker: {
    health: ReturnType<typeof vi.fn>;
    diagnose: ReturnType<typeof vi.fn>;
    dataPlaneHealth: ReturnType<typeof vi.fn>;
    ibkrApiEvidence: ReturnType<typeof vi.fn>;
  };

  beforeEach(() => {
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
    broker = {
      health: vi.fn().mockResolvedValue(connectionHealth()),
      diagnose: vi.fn().mockResolvedValue(diagnose()),
      dataPlaneHealth: vi.fn().mockResolvedValue(dataPlaneHealth()),
      ibkrApiEvidence: vi.fn().mockResolvedValue([evidence(1)]),
    };
    TestBed.configureTestingModule({
      imports: [IbkrApiEvidencePanelComponent],
      providers: [
        provideZonelessChangeDetection(),
        { provide: BrokerService, useValue: broker },
      ],
    });
  });

  afterEach(() => {
    vi.unstubAllGlobals();
  });

  async function render(): Promise<ComponentFixture<IbkrApiEvidencePanelComponent>> {
    const fixture = TestBed.createComponent(IbkrApiEvidencePanelComponent);
    fixture.detectChanges();
    await fixture.whenStable();
    await Promise.resolve();
    await Promise.resolve();
    fixture.detectChanges();
    return fixture;
  }

  it('loads data-plane health, broker health, diagnose, evidence backfill, and opens the evidence stream', async () => {
    const fixture = await render();
    const text = fixture.nativeElement.textContent as string;

    expect(broker.dataPlaneHealth).toHaveBeenCalledTimes(1);
    expect(broker.health).toHaveBeenCalledTimes(1);
    expect(broker.diagnose).toHaveBeenCalledTimes(1);
    expect(broker.ibkrApiEvidence).toHaveBeenCalledWith(0, 120);
    expect(FakeEventSource.instances[0]?.url).toBe(
      '/api/broker/ibkr/evidence/stream?since_seq=1',
    );
    expect(text).toContain('8398d285978a');
    expect(text).toContain('watchfiles-polling');
    expect(text).toContain('DU1234567');
    expect(text).toContain('IB Gateway');
    expect(text).toContain('reqPositionsAsync');
  });

  it('highlights serializer placeholders from backfill and live stream events', async () => {
    broker.ibkrApiEvidence.mockResolvedValue([
      evidence(4, 'Cannot snapshot unsupported IBKR evidence object OpaqueIbkrObject'),
    ]);
    const fixture = await render();

    FakeEventSource.instances[0]?.emit(
      'ibkr_api',
      evidence(5, 'Cannot snapshot unsupported IBKR evidence object OtherOpaqueObject'),
    );
    fixture.detectChanges();

    const text = fixture.nativeElement.textContent as string;
    expect(text).toContain('Serializer warning');
    expect(text).toContain('OpaqueIbkrObject');
    expect(text).toContain('OtherOpaqueObject');
  });

  it('renders successful diagnostics when one snapshot probe fails', async () => {
    broker.diagnose.mockRejectedValue(new Error('diagnose failed'));

    const fixture = await render();
    const text = fixture.nativeElement.textContent as string;

    expect(text).toContain('Some broker diagnostics failed to load.');
    expect(text).toContain('8398d285978a');
    expect(text).toContain('DU1234567');
    expect(text).toContain('reqPositionsAsync');
    expect(FakeEventSource.instances[0]?.url).toBe(
      '/api/broker/ibkr/evidence/stream?since_seq=1',
    );
  });

  it('does not open the evidence stream after the component is destroyed mid-load', async () => {
    const dataPlane = deferred<DataPlaneHealth>();
    broker.dataPlaneHealth.mockReturnValue(dataPlane.promise);
    const fixture = TestBed.createComponent(IbkrApiEvidencePanelComponent);
    fixture.detectChanges();

    fixture.destroy();
    dataPlane.resolve(dataPlaneHealth());
    await fixture.whenStable();
    await Promise.resolve();
    await Promise.resolve();

    expect(FakeEventSource.instances).toEqual([]);
  });
});
