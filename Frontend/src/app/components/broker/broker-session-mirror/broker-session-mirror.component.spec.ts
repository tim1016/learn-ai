import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  BrokerSessionMirrorSnapshot,
  BrokerSessionRosterRow,
} from '../../../api/broker-session-mirror.types';
import { BrokerSessionMirrorService } from '../../../services/broker-session-mirror.service';
import { BrokerSessionMirrorComponent } from './broker-session-mirror.component';

const AS_OF_MS = 1_783_120_000_000;

class FakeEventSource {
  static instances: FakeEventSource[] = [];

  readonly listeners = new Map<string, ((event: Event) => void)[]>();
  closed = false;

  constructor(readonly url: string) {
    FakeEventSource.instances.push(this);
  }

  addEventListener(type: string, listener: (event: Event) => void): void {
    const listeners = this.listeners.get(type) ?? [];
    listeners.push(listener);
    this.listeners.set(type, listeners);
  }

  close(): void {
    this.closed = true;
  }

  emit(type: string, data: string): void {
    const event = new MessageEvent(type, { data });
    for (const listener of this.listeners.get(type) ?? []) {
      listener(event);
    }
  }
}

class FakeBrokerSessionMirrorService {
  snapshot = vi.fn<() => Promise<BrokerSessionMirrorSnapshot>>();
}

describe('BrokerSessionMirrorComponent', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    FakeEventSource.instances = [];
    vi.stubGlobal('EventSource', FakeEventSource);
  });

  it('renders the mirror roster from the REST snapshot', async () => {
    const { fixture } = await setup(snapshot({ rows: [botSocket()] }));

    const text = pageText(fixture);
    expect(text).toContain('PrajiTSLADemo');
    expect(text).toContain('Bot');
    expect(text).toContain('CURRENT');
    expect(text).toContain('Registry offline; socket live');
  });

  it('updates rows from the SSE snapshot stream', async () => {
    const { fixture } = await setup(snapshot({ rows: [] }));

    FakeEventSource.instances[0].emit(
      'snapshot',
      JSON.stringify(snapshot({ rows: [ghostSocket()] })),
    );
    await settle(fixture);

    const text = pageText(fixture);
    expect(text).toContain('external');
    expect(text).toContain('Ghost');
    expect(text).toContain('Unattributed socket');
  });

  it('renders past rows as PAST and never as CURRENT', async () => {
    const { fixture } = await setup(
      snapshot({
        rows: [
          botSocket({
            recency: 'past_last_known',
            socket_present: false,
            attention_codes: [],
          }),
        ],
      }),
    );

    const text = pageText(fixture);
    expect(text).toContain('PAST');
    expect(text).not.toContain('CURRENT');
  });

  it('surfaces degraded observer and unknown ghost detection states', async () => {
    const { fixture } = await setup(
      snapshot({
        observer_status: 'degraded',
        ghost_detection_status: 'unknown',
        degradation_reasons: ['host daemon socket probe unavailable'],
      }),
    );

    const text = pageText(fixture);
    expect(text).toContain('Observer degraded');
    expect(text).toContain('Ghost detection unknown');
    expect(text).toContain('host daemon socket probe unavailable');
  });

  it('navigates to the Bot Cockpit for attributed bot rows', async () => {
    const { fixture, router } = await setup(snapshot({ rows: [botSocket()] }));
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    const button = fixture.nativeElement.querySelector(
      '[aria-label="Open bot PrajiTSLADemo"]',
    ) as HTMLButtonElement | null;
    expect(button).toBeDefined();
    button?.click();
    await settle(fixture);

    expect(navigate).toHaveBeenCalledWith(['/broker/bots', 'PrajiTSLADemo']);
  });
});

async function setup(initialSnapshot: BrokerSessionMirrorSnapshot): Promise<{
  fixture: ComponentFixture<BrokerSessionMirrorComponent>;
  service: FakeBrokerSessionMirrorService;
  router: Router;
}> {
  const service = new FakeBrokerSessionMirrorService();
  service.snapshot.mockResolvedValue(initialSnapshot);

  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [BrokerSessionMirrorComponent],
    providers: [
      provideZonelessChangeDetection(),
      provideRouter([]),
      { provide: BrokerSessionMirrorService, useValue: service },
    ],
  }).compileComponents();

  const fixture = TestBed.createComponent(BrokerSessionMirrorComponent);
  await settle(fixture);
  return { fixture, service, router: TestBed.inject(Router) };
}

async function settle(
  fixture: ComponentFixture<BrokerSessionMirrorComponent>,
): Promise<void> {
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
}

function pageText(
  fixture: ComponentFixture<BrokerSessionMirrorComponent>,
): string {
  return (fixture.nativeElement as HTMLElement).textContent ?? '';
}

function snapshot(
  overrides: Partial<BrokerSessionMirrorSnapshot> = {},
): BrokerSessionMirrorSnapshot {
  return {
    as_of_ms: AS_OF_MS,
    gateway_port: 4002,
    observer_status: 'online',
    ghost_detection_status: 'available',
    rows: [],
    degradation_reasons: [],
    ...overrides,
  };
}

function botSocket(
  overrides: Partial<BrokerSessionRosterRow> = {},
): BrokerSessionRosterRow {
  return {
    row_id: 'socket:21760:50123:4002:0',
    identity_type: 'bot',
    recency: 'current',
    socket_present: true,
    strategy_instance_id: 'PrajiTSLADemo',
    run_id: 'run-a',
    account_id: 'DU123',
    posture: 'PAPER_EXECUTION',
    client_id: null,
    pid: 21760,
    command: 'python',
    run_dir: '/runs/run-a',
    local_port: 50123,
    remote_host: '127.0.0.1',
    remote_port: 4002,
    connection_state: 'connected',
    recovery_state: null,
    connection_epoch: 0,
    last_event_ms: AS_OF_MS - 500,
    as_of_ms: AS_OF_MS,
    attention_codes: ['REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE'],
    registry_claim: {
      state: 'exited',
      run_id: 'run-a',
      pid: 21760,
      run_dir: '/runs/run-a',
      started_at_ms: AS_OF_MS - 60_000,
      ended_at_ms: AS_OF_MS - 1_000,
    },
    ...overrides,
  };
}

function ghostSocket(): BrokerSessionRosterRow {
  return {
    ...botSocket({
      row_id: 'socket:999:50126:4002:0',
      identity_type: 'ghost',
      strategy_instance_id: null,
      run_id: null,
      account_id: null,
      pid: 999,
      command: 'external',
      run_dir: null,
      connection_state: null,
      attention_codes: ['GHOST_SOCKET'],
      registry_claim: null,
    }),
  };
}
