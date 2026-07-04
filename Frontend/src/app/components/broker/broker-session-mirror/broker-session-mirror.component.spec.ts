import { provideZonelessChangeDetection } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  BrokerSessionEvent,
  BrokerSessionEventPurgeRequest,
  BrokerSessionEventPurgeResult,
  BrokerSessionHistoryPage,
  BrokerSessionHistoryPurgeRequest,
  BrokerSessionHistoryPurgeResult,
  BrokerSessionMirrorSnapshot,
  BrokerSessionRosterRow,
} from '../../../api/broker-session-mirror.types';
import { BROKER_SESSION_PURGE_CONFIRM } from '../../../api/broker-session-mirror.types';
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
  history = vi.fn<(params?: { limit?: number }) => Promise<BrokerSessionHistoryPage>>();
  purgeHistory =
    vi.fn<
      (request: BrokerSessionHistoryPurgeRequest) => Promise<BrokerSessionHistoryPurgeResult>
    >();
  purgeEvents =
    vi.fn<
      (request: BrokerSessionEventPurgeRequest) => Promise<BrokerSessionEventPurgeResult>
    >();
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
    expect(text).toContain('Healthy');
    expect(text).toContain('Registry offline; socket live');
  });

  it('updates rows from the SSE snapshot stream', async () => {
    const { fixture } = await setup(snapshot({ rows: [] }));

    eventSourceFor('/api/broker/session-mirror/stream').emit(
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

  it('renders recovery states separately from broker connection labels', async () => {
    const { fixture } = await setup(
      snapshot({
        rows: [
          botSocket({
            connection_state: 'hard_down',
            recovery_state: 'HARD_DOWN',
          }),
        ],
      }),
    );

    const text = pageText(fixture);
    expect(text).toContain('hard_down');
    expect(text).toContain('Hard down');
  });

  it('renders retained broker-session history snapshots', async () => {
    const { fixture, service } = await setup(snapshot({ rows: [] }));
    service.history.mockResolvedValueOnce({
      retained_count: 1,
      rows: [
        snapshot({
          as_of_ms: AS_OF_MS - 60_000,
          rows: [
            botSocket({
              strategy_instance_id: 'ClosedBot',
              recency: 'past_closed',
              socket_present: false,
              client_id: 88,
              attention_codes: [],
            }),
          ],
        }),
      ],
    });

    const button = fixture.nativeElement.querySelector(
      '[data-testid="history-refresh"]',
    ) as HTMLButtonElement | null;
    button?.click();
    await settle(fixture);

    const text = pageText(fixture);
    expect(service.history).toHaveBeenCalledWith({ limit: 12 });
    expect(text).toContain('Recent history');
    expect(text).toContain('1 retained');
    expect(text).toContain('ClosedBot');
    expect(text).toContain('PAST');
    expect(text).toContain('client 88');
  });

  it('surfaces degraded observer and unknown ghost detection states', async () => {
    const { fixture } = await setup(
      snapshot({
        observer_status: 'degraded',
        ghost_detection_status: 'unknown',
        degradation_reasons: ['host daemon socket probe unavailable'],
        rows: [
          botSocket({
            recency: 'past_last_known',
            socket_present: false,
            attention_codes: [
              'GHOST_DETECTION_UNAVAILABLE',
              'CLIENT_SIGNAL_STALE',
            ],
          }),
        ],
      }),
    );

    const text = pageText(fixture);
    expect(text).toContain('Observer degraded');
    expect(text).toContain('Ghost detection unknown');
    expect(text).toContain('host daemon socket probe unavailable');
    expect(text).toContain('PAST');
    expect(text).toContain('Client signal stale');
  });

  it('renders categorized broker events in row detail', async () => {
    const { fixture } = await setup(
      snapshot({
        rows: [
          botSocket({
            client_id: 42,
            event_counts: { link_connectivity: 1 },
          }),
        ],
      }),
    );

    eventSourceFor('/api/broker/session-mirror/events/stream').emit(
      'broker_event',
      JSON.stringify(
        brokerEvent({
          client_id: 42,
          category: 'link_connectivity',
          severity: 'warning',
          label: 'IBKR link interrupted',
          ibkr_code: 1100,
          message: 'Connectivity between IB and TWS has been lost',
        }),
      ),
    );
    await settle(fixture);

    const text = pageText(fixture);
    expect(text).toContain('Link connectivity');
    expect(text).toContain('IBKR link interrupted');
    expect(text).toContain('Connectivity between IB and TWS has been lost');
    expect(text).toContain('1100');
  });

  it('renders backend-authored orphan notices and opens the owning cockpit', async () => {
    const { fixture, router } = await setup(
      snapshot({
        rows: [
          botSocket({
            identity_type: 'orphaned_bot_socket',
            pid: null,
            client_id: 17,
            attention_codes: ['SOCKET_WITHOUT_LIVE_PID', 'ORPHANED_BOT_SOCKET'],
            notice: orphanNotice(),
          }),
        ],
      }),
    );
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    const text = pageText(fixture);
    expect(text).toContain('Orphaned broker socket detected');
    expect(text).toContain('Verify the client session in IBKR');

    const button = Array.from(
      (fixture.nativeElement as HTMLElement).querySelectorAll('button'),
    ).find((candidate) => candidate.textContent?.includes('Open Bot Cockpit'));
    expect(button).toBeDefined();
    (button as HTMLButtonElement | undefined)?.click();
    await settle(fixture);

    expect(navigate).toHaveBeenCalledWith(['/broker/bots', 'PrajiTSLADemo']);
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

  it('purges diagnostic history with typed confirmation and clears buffered events', async () => {
    const { fixture, service } = await setup(
      snapshot({
        rows: [
          botSocket({
            client_id: 42,
            event_counts: { link_connectivity: 1 },
          }),
        ],
      }),
    );
    service.purgeEvents.mockResolvedValue({
      purged_count: 1,
      remaining_count: 0,
    });
    eventSourceFor('/api/broker/session-mirror/events/stream').emit(
      'broker_event',
      JSON.stringify(
        brokerEvent({
          client_id: 42,
          category: 'link_connectivity',
          label: 'IBKR link interrupted',
        }),
      ),
    );
    await settle(fixture);
    expect(pageText(fixture)).toContain('IBKR link interrupted');

    writeInput(fixture, 'purge-client-id', '42');
    writeInput(fixture, 'purge-confirm', BROKER_SESSION_PURGE_CONFIRM);
    const button = fixture.nativeElement.querySelector(
      '[data-testid="purge-submit"]',
    ) as HTMLButtonElement | null;
    expect(button?.disabled).toBe(false);
    button?.click();
    await settle(fixture);

    expect(service.purgeEvents).toHaveBeenCalledWith({
      client_id: 42,
      start_ms: null,
      end_ms: null,
      confirm: BROKER_SESSION_PURGE_CONFIRM,
    });
    expect(pageText(fixture)).toContain('Purged 1 event; 0 remain.');
    expect(pageText(fixture)).not.toContain('IBKR link interrupted');
  });

  it('purges roster history with typed confirmation without clearing buffered events', async () => {
    const { fixture, service } = await setup(
      snapshot({
        rows: [
          botSocket({
            client_id: 42,
            event_counts: { link_connectivity: 1 },
          }),
        ],
      }),
    );
    service.purgeHistory.mockResolvedValue({
      purged_row_count: 2,
      purged_snapshot_count: 1,
      remaining_snapshot_count: 3,
    });
    eventSourceFor('/api/broker/session-mirror/events/stream').emit(
      'broker_event',
      JSON.stringify(
        brokerEvent({
          client_id: 42,
          category: 'link_connectivity',
          label: 'IBKR link interrupted',
        }),
      ),
    );
    await settle(fixture);

    clickButton(fixture, 'purge-target-history');
    writeInput(fixture, 'purge-client-id', '42');
    writeInput(fixture, 'purge-confirm', BROKER_SESSION_PURGE_CONFIRM);
    clickButton(fixture, 'purge-submit');
    await settle(fixture);

    expect(service.purgeHistory).toHaveBeenCalledWith({
      client_id: 42,
      start_ms: null,
      end_ms: null,
      confirm: BROKER_SESSION_PURGE_CONFIRM,
    });
    expect(service.purgeEvents).not.toHaveBeenCalled();
    expect(service.history).toHaveBeenLastCalledWith({ limit: 12 });
    expect(pageText(fixture)).toContain(
      'Purged 2 history rows; 1 snapshot removed; 3 snapshots remain.',
    );
    expect(pageText(fixture)).toContain('IBKR link interrupted');
  });
});

async function setup(initialSnapshot: BrokerSessionMirrorSnapshot): Promise<{
  fixture: ComponentFixture<BrokerSessionMirrorComponent>;
  service: FakeBrokerSessionMirrorService;
  router: Router;
}> {
  const service = new FakeBrokerSessionMirrorService();
  service.snapshot.mockResolvedValue(initialSnapshot);
  service.history.mockResolvedValue({ retained_count: 0, rows: [] });
  service.purgeHistory.mockResolvedValue({
    purged_row_count: 0,
    purged_snapshot_count: 0,
    remaining_snapshot_count: 0,
  });
  service.purgeEvents.mockResolvedValue({ purged_count: 0, remaining_count: 0 });

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

function eventSourceFor(url: string): FakeEventSource {
  const source = FakeEventSource.instances.find((candidate) =>
    candidate.url.includes(url),
  );
  if (source === undefined) {
    throw new Error(`EventSource not opened for ${url}`);
  }
  return source;
}

function writeInput(
  fixture: ComponentFixture<BrokerSessionMirrorComponent>,
  testId: string,
  value: string,
): void {
  const input = fixture.nativeElement.querySelector(
    `[data-testid="${testId}"]`,
  ) as HTMLInputElement | null;
  if (input === null) throw new Error(`input not found: ${testId}`);
  input.value = value;
  input.dispatchEvent(new Event('input'));
  fixture.detectChanges();
}

function clickButton(
  fixture: ComponentFixture<BrokerSessionMirrorComponent>,
  testId: string,
): void {
  const button = fixture.nativeElement.querySelector(
    `[data-testid="${testId}"]`,
  ) as HTMLButtonElement | null;
  if (button === null) throw new Error(`button not found: ${testId}`);
  button.click();
  fixture.detectChanges();
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
    recovery_state: 'HEALTHY',
    connection_epoch: 0,
    last_event_ms: AS_OF_MS - 500,
    as_of_ms: AS_OF_MS,
    event_counts: {},
    attention_codes: ['REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE'],
    registry_claim: {
      state: 'exited',
      run_id: 'run-a',
      pid: 21760,
      run_dir: '/runs/run-a',
      started_at_ms: AS_OF_MS - 60_000,
      ended_at_ms: AS_OF_MS - 1_000,
    },
    notice: null,
    ...overrides,
  };
}

function brokerEvent(
  overrides: Partial<BrokerSessionEvent> = {},
): BrokerSessionEvent {
  return {
    seq: 1,
    ts_ms: AS_OF_MS,
    category: 'client_lifecycle',
    severity: 'info',
    label: 'Broker probe succeeded',
    message: null,
    raw_event_type: 'BROKER_PROBE_OK',
    client_id: 42,
    account_id: 'DU123',
    ibkr_code: null,
    connection_state: 'connected',
    raw: {},
    ...overrides,
  };
}

function orphanNotice(): BrokerSessionRosterRow['notice'] {
  return {
    code: 'broker_session.orphaned_socket',
    tier: 'critical',
    title: 'Orphaned broker socket detected',
    message:
      'IB Gateway still shows a broker socket for PrajiTSLADemo, but the host process is not live. Verify the client session in IBKR and reconcile broker orders and positions before restarting this bot.',
    source_codes: ['SOCKET_WITHOUT_LIVE_PID', 'ORPHANED_BOT_SOCKET'],
    forensic_facts: {
      strategy_instance_id: 'PrajiTSLADemo',
      run_id: 'run-a',
      client_id: 17,
      observed_at_ms: AS_OF_MS,
    },
    action: {
      kind: 'focus_cockpit_action',
      label: 'Open Bot Cockpit',
      target: 'PrajiTSLADemo',
    },
    runbook_slug: 'broker-session-orphaned-socket',
    occurred_at_ms: AS_OF_MS,
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
