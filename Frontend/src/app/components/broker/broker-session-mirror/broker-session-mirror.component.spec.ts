import { provideZonelessChangeDetection, signal } from '@angular/core';
import { TestBed, type ComponentFixture } from '@angular/core/testing';
import { provideRouter, Router } from '@angular/router';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type {
  DaemonDiagnosticReport,
} from '../../../api/daemon-diagnostics.types';
import type {
  BrokerSessionEventPurgeRequest,
  BrokerSessionEventPurgeResult,
  BrokerSessionHistoryPage,
  BrokerSessionHistoryPurgeRequest,
  BrokerSessionHistoryPurgeResult,
  BrokerSessionMirrorSnapshot,
} from '../../../api/broker-session-mirror.types';
import { BROKER_SESSION_PURGE_CONFIRM } from '../../../api/broker-session-mirror.types';
import { BrokerSessionMirrorService } from '../../../services/broker-session-mirror.service';
import { DaemonDiagnosticsStore } from '../../../services/daemon-diagnostics-store.service';
import {
  AS_OF_MS,
  botSocket,
  brokerEvent,
  ghostSocket,
  historyBot,
  orphanNotice,
  snapshot,
} from './broker-session-mirror.fixtures';
import { BrokerSessionMirrorComponent } from './broker-session-mirror.component';

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

class FakeDaemonDiagnosticsStore {
  private readonly _report = signal<DaemonDiagnosticReport | null>(null);
  private readonly _loading = signal<boolean>(false);
  private readonly _error = signal<string | null>(null);

  readonly report = this._report.asReadonly();
  readonly loading = this._loading.asReadonly();
  readonly error = this._error.asReadonly();
  readonly refresh = vi.fn(async () => {
    this._report.set(daemonDiagnosticsReport());
  });
  readonly renewLease = vi.fn(async () => {
    this._report.set(daemonDiagnosticsReport());
  });
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
    expect(text).toContain('Bot session');
    expect(text).toContain('Live now');
    expect(text).toContain('Healthy');
    expect(text).toContain('Registry offline; socket live');
  });

  it('embeds the latest daemon diagnostics header without probing on page load', async () => {
    const { fixture, daemonDiagnostics } = await setup(snapshot({ rows: [] }));

    expect(daemonDiagnostics.refresh).not.toHaveBeenCalled();
    expect(pageText(fixture)).toContain('No daemon diagnostics snapshot has been loaded.');

    daemonDiagnosticsButtonByText(fixture, 'Refresh').click();
    await settle(fixture);

    expect(daemonDiagnostics.refresh).toHaveBeenCalledOnce();
    expect(pageText(fixture)).toContain('Live engine diagnostics are clear');
  });

  it('keeps technical identifiers out of the main row until expanded', async () => {
    const row = botSocket({ run_id: 'commit-like-run-hash-123' });
    const { fixture } = await setup(snapshot({ rows: [row] }));

    expect(pageText(fixture)).toContain('PrajiTSLADemo');
    expect(pageText(fixture)).not.toContain('commit-like-run-hash-123');

    expandRow(fixture, row.row_id);
    await settle(fixture);

    expect(pageText(fixture)).toContain('Technical detail');
    expect(pageText(fixture)).toContain('commit-like-run-hash-123');
    expect(pageText(fixture)).toContain(row.row_id);
  });

  it('updates rows from the SSE snapshot stream', async () => {
    const { fixture } = await setup(snapshot({ rows: [] }));

    eventSourceFor('/api/broker/session-mirror/stream').emit(
      'snapshot',
      JSON.stringify(snapshot({ rows: [ghostSocket()] })),
    );
    await settle(fixture);

    const text = pageText(fixture);
    expect(text).not.toContain('external');
    expect(text).toContain('Unattributed broker socket');
  });

  it('marks protected SSE streams with the browser control intent query', async () => {
    await setup(snapshot({ rows: [] }));

    expect(eventSourceFor('/api/broker/session-mirror/stream').url).toContain(
      'control_intent=learn-ai-browser-control',
    );
    expect(
      eventSourceFor('/api/broker/session-mirror/events/stream').url,
    ).toContain('control_intent=learn-ai-browser-control');
  });

  it('keeps a newer manual refresh when an older SSE snapshot arrives', async () => {
    const { fixture } = await setup(snapshot({ rows: [botSocket()] }));

    eventSourceFor('/api/broker/session-mirror/stream').emit(
      'snapshot',
      JSON.stringify(
        snapshot({
          as_of_ms: AS_OF_MS - 1_000,
          rows: [ghostSocket()],
        }),
      ),
    );
    await settle(fixture);

    const text = pageText(fixture);
    expect(text).toContain('PrajiTSLADemo');
    expect(text).not.toContain('external');
  });

  it('renders past rows as last-known and never as live-now', async () => {
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
    expect(text).toContain('Last known');
    expect(text).not.toContain('Live now');
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
    expect(text).toContain('Broker recovery exhausted');
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
    expect(text).toContain('1 row');
    expect(text).not.toContain('ClosedBot');

    clickHistoryHeader(fixture, AS_OF_MS - 60_000);
    await settle(fixture);

    const openText = pageText(fixture);
    expect(openText).toContain('ClosedBot');
    expect(openText).toContain('Past session');
    expect(openText).toContain('client 88');
  });

  it('expands and collapses retained history snapshot details', async () => {
    const snapshotMs = AS_OF_MS - 60_000;
    const { fixture, service } = await setup(snapshot({ rows: [] }));
    service.history.mockResolvedValueOnce({
      retained_count: 1,
      rows: [
        snapshot({
          as_of_ms: snapshotMs,
          rows: [
            historyBot('HistoryBot1', 81),
            historyBot('HistoryBot2', 82),
            historyBot('HistoryBot3', 83),
            historyBot('HistoryBot4', 84),
            historyBot('HistoryBot5', 85),
          ],
        }),
      ],
    });

    clickButton(fixture, 'history-refresh');
    await settle(fixture);

    expect(pageText(fixture)).toContain('5 rows');
    expect(pageText(fixture)).not.toContain('HistoryBot1');

    clickHistoryHeader(fixture, snapshotMs);
    await settle(fixture);

    expect(pageText(fixture)).toContain('HistoryBot1');
    expect(pageText(fixture)).toContain('HistoryBot5');

    clickHistoryHeader(fixture, snapshotMs);
    await settle(fixture);

    expect(pageText(fixture)).not.toContain('HistoryBot5');
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

    let text = pageText(fixture);
    expect(text).toContain('Observer degraded');
    expect(text).toContain('Ghost detection unknown');
    expect(text).toContain('host daemon socket probe unavailable');
    expect(text).toContain('Last known');
    expect(text).toContain('+1');
    expect(text).not.toContain('Client signal stale');

    expandRow(fixture);
    await settle(fixture);
    text = pageText(fixture);
    expect(text).toContain('Client signal stale');
  });

  it('renders global network events outside the broker session roster', async () => {
    const { fixture } = await setup(
      snapshot({
        global_events: [
          {
            code: 'GATEWAY_NETWORK_PROXY',
            label: 'Gateway network proxy',
            severity: 'info',
            summary: 'A virtual-machine network proxy is connected to the IBKR gateway port.',
            current: true,
            source: 'network',
            observed_at_ms: AS_OF_MS,
            client_id: null,
          },
          {
            code: 'DATA_PLANE_BROKER_CLIENT',
            label: 'Data-plane broker client',
            severity: 'neutral',
            summary: 'The data-plane IBKR client is not connected; this global fact is separate from bot-owned sessions.',
            current: false,
            source: 'data_plane',
            observed_at_ms: AS_OF_MS - 500,
            client_id: 42,
          },
        ],
        rows: [botSocket()],
      }),
    );

    const text = pageText(fixture);
    expect(text).toContain('Gateway network proxy: on');
    expect(text).toContain('Data-plane broker client: off');
    expect(text).toContain('PrajiTSLADemo');
  });

  it('renders categorized broker events in row detail', async () => {
    const { fixture } = await setup(
      snapshot({
        rows: [
          botSocket({
            client_id: 42,
            event_counts: { link_connectivity: 1 },
            events: [
              brokerEvent({
                client_id: 42,
                category: 'link_connectivity',
                severity: 'warning',
                label: 'IBKR link interrupted',
                ibkr_code: 1100,
                message: 'Connectivity between IB and TWS has been lost',
              }),
            ],
          }),
        ],
      }),
    );

    expandRow(fixture);
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

    let text = pageText(fixture);
    expect(text).toContain('Open Bot Cockpit');
    expect(text).not.toContain('Verify the client session in IBKR');

    buttonByText(fixture, 'Open Bot Cockpit').click();
    await settle(fixture);

    expect(navigate).toHaveBeenCalledWith(['/broker/bots', 'PrajiTSLADemo']);

    expandRow(fixture);
    await settle(fixture);

    text = pageText(fixture);
    expect(text).toContain('Orphaned broker socket detected');
    expect(text).toContain('Verify the client session in IBKR');
  });

  it('navigates to the Bot Cockpit for attributed bot rows', async () => {
    const { fixture, router } = await setup(snapshot({ rows: [botSocket()] }));
    const navigate = vi.spyOn(router, 'navigate').mockResolvedValue(true);

    const button = fixture.nativeElement.querySelector(
      '[aria-label="Open bot PrajiTSLADemo"]',
    ) as HTMLButtonElement | null;
    expect(button).not.toBeNull();
    button?.click();
    await settle(fixture);

    expect(navigate).toHaveBeenCalledWith(['/broker/bots', 'PrajiTSLADemo']);
  });

  it('purges diagnostic events with typed confirmation and refreshes authored row events', async () => {
    const firstRow = botSocket({
      client_id: 42,
      event_counts: { link_connectivity: 1 },
      events: [
        brokerEvent({
          client_id: 42,
          category: 'link_connectivity',
          label: 'IBKR link interrupted',
        }),
      ],
    });
    const secondRow = botSocket({
      row_id: 'socket:21761:50124:4002:0',
      strategy_instance_id: 'OtherBot',
      run_id: 'run-b',
      client_id: 77,
      event_counts: { data_farm: 1 },
      events: [
        brokerEvent({
          seq: 2,
          client_id: 77,
          category: 'data_farm',
          label: 'Market data farm degraded',
        }),
      ],
      registry_claim: {
        state: 'running',
        run_id: 'run-b',
        pid: 21761,
        run_dir: '/runs/run-b',
        started_at_ms: AS_OF_MS - 60_000,
        ended_at_ms: null,
      },
    });
    const { fixture, service } = await setup(
      snapshot({
        rows: [firstRow, secondRow],
      }),
    );
    service.purgeEvents.mockResolvedValue({
      purged_count: 1,
      remaining_count: 0,
    });
    service.snapshot.mockResolvedValueOnce(
      snapshot({
        rows: [
          {
            ...firstRow,
            event_counts: {},
            events: [],
          },
          secondRow,
        ],
      }),
    );
    expandRow(fixture, 'socket:21760:50123:4002:0');
    await settle(fixture);
    expandRow(fixture, 'socket:21761:50124:4002:0');
    await settle(fixture);
    expect(pageText(fixture)).toContain('IBKR link interrupted');
    expect(pageText(fixture)).toContain('Market data farm degraded');

    writeInput(fixture, 'purge-client-id', '42');
    writeInput(fixture, 'purge-confirm', BROKER_SESSION_PURGE_CONFIRM);
    const button = fixture.nativeElement.querySelector(
      '[data-testid="purge-submit"]',
    ) as HTMLButtonElement | null;
    expect(button?.disabled).toBe(false);
    button?.click();
    await settle(fixture);
    await settle(fixture);

    expect(service.purgeEvents).toHaveBeenCalledWith({
      client_id: 42,
      start_ms: null,
      end_ms: null,
      confirm: BROKER_SESSION_PURGE_CONFIRM,
    });
    expect(pageText(fixture)).toContain('Purged 1 event; 0 remain.');
    expect(pageText(fixture)).not.toContain('IBKR link interrupted');
    expect(pageText(fixture)).toContain('Market data farm degraded');
  });

  it('renders backend-scoped row detail events without matching buffered stream events', async () => {
    const { fixture } = await setup(
      snapshot({
        rows: [
          botSocket({
            client_id: 42,
            as_of_ms: AS_OF_MS,
            event_counts: { link_connectivity: 1 },
            events: [
              brokerEvent({
                seq: 2,
                ts_ms: AS_OF_MS - 50,
                client_id: 42,
                label: 'Current link event',
              }),
            ],
            registry_claim: {
              state: 'running',
              run_id: 'run-a',
              pid: 21760,
              run_dir: '/runs/run-a',
              started_at_ms: AS_OF_MS - 100,
              ended_at_ms: null,
            },
          }),
          botSocket({
            row_id: 'socket:21761:50124:4002:0',
            strategy_instance_id: 'MissingStart',
            run_id: 'run-missing-start',
            client_id: 77,
            event_counts: { link_connectivity: 1 },
            registry_claim: null,
          }),
        ],
      }),
    );

    const eventSource = eventSourceFor(
      '/api/broker/session-mirror/events/stream',
    );
    eventSource.emit(
      'broker_event',
      JSON.stringify(
        brokerEvent({
          seq: 1,
          ts_ms: AS_OF_MS - 200,
          client_id: 42,
          label: 'Old link event',
        }),
      ),
    );
    eventSource.emit(
      'broker_event',
      JSON.stringify(
        brokerEvent({
          seq: 3,
          ts_ms: AS_OF_MS + 50,
          client_id: 42,
          label: 'Future link event',
        }),
      ),
    );
    eventSource.emit(
      'broker_event',
      JSON.stringify(
        brokerEvent({
          seq: 4,
          ts_ms: AS_OF_MS - 50,
          client_id: 77,
          label: 'Missing start event',
        }),
      ),
    );
    await settle(fixture);
    expandRow(fixture);
    await settle(fixture);

    const text = pageText(fixture);
    expect(text).toContain('Current link event');
    expect(text).not.toContain('Old link event');
    expect(text).not.toContain('Future link event');
    expect(text).not.toContain('Missing start event');
  });

  it('purges roster history with typed confirmation without clearing authored row events', async () => {
    const { fixture, service } = await setup(
      snapshot({
        rows: [
          botSocket({
            client_id: 42,
            event_counts: { link_connectivity: 1 },
            events: [
              brokerEvent({
                client_id: 42,
                category: 'link_connectivity',
                label: 'IBKR link interrupted',
              }),
            ],
          }),
        ],
      }),
    );
    service.purgeHistory.mockResolvedValue({
      purged_row_count: 2,
      purged_snapshot_count: 1,
      remaining_snapshot_count: 3,
    });
    expandRow(fixture);
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
  daemonDiagnostics: FakeDaemonDiagnosticsStore;
  router: Router;
}> {
  const service = new FakeBrokerSessionMirrorService();
  const daemonDiagnostics = new FakeDaemonDiagnosticsStore();
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
      { provide: DaemonDiagnosticsStore, useValue: daemonDiagnostics },
    ],
  }).compileComponents();

  const fixture = TestBed.createComponent(BrokerSessionMirrorComponent);
  await settle(fixture);
  return { fixture, service, daemonDiagnostics, router: TestBed.inject(Router) };
}

function daemonDiagnosticsReport(): DaemonDiagnosticReport {
  return {
    overall_status: 'pass',
    transport: 'CONNECTED',
    dominant_condition: 'healthy',
    headline: {
      title: 'Live engine diagnostics are clear',
      summary: 'No daemon-control-plane fault was found in this snapshot.',
      remediation: null,
    },
    checks: [],
    per_instance: [],
    daemon_boot_id: 'boot-1',
    fetched_at_ms: AS_OF_MS,
  };
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

function clickHistoryHeader(
  fixture: ComponentFixture<BrokerSessionMirrorComponent>,
  asOfMs: number,
): void {
  const header = fixture.nativeElement.querySelector(
    `[data-testid="history-panel-header-${asOfMs}"]`,
  ) as HTMLElement | null;
  if (header === null) throw new Error(`history header not found: ${asOfMs}`);
  const trigger = header.querySelector('button') ?? header;
  trigger.click();
  fixture.detectChanges();
}

function buttonByText(
  fixture: ComponentFixture<BrokerSessionMirrorComponent>,
  text: string,
): HTMLButtonElement {
  const button = Array.from(
    (fixture.nativeElement as HTMLElement).querySelectorAll<HTMLButtonElement>(
      'button',
    ),
  ).find((candidate) => candidate.textContent?.includes(text));
  if (button === undefined) throw new Error(`button not found: ${text}`);
  return button;
}

function daemonDiagnosticsButtonByText(
  fixture: ComponentFixture<BrokerSessionMirrorComponent>,
  text: string,
): HTMLButtonElement {
  const header = (fixture.nativeElement as HTMLElement).querySelector(
    '.control-plane-header',
  );
  const button = Array.from(
    header?.querySelectorAll<HTMLButtonElement>('button') ?? [],
  ).find((candidate) => candidate.textContent?.includes(text));
  if (button === undefined) {
    throw new Error(`daemon diagnostics button not found: ${text}`);
  }
  return button;
}

function expandRow(
  fixture: ComponentFixture<BrokerSessionMirrorComponent>,
  rowId = 'socket:21760:50123:4002:0',
): void {
  clickButton(fixture, `row-toggle-${rowId}`);
}
