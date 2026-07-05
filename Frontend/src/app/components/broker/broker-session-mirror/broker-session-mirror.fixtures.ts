import type {
  BrokerSessionEvent,
  BrokerSessionMirrorSnapshot,
  BrokerSessionRosterRow,
} from '../../../api/broker-session-mirror.types';

export const AS_OF_MS = 1_783_120_000_000;

export function snapshot(
  overrides: Partial<BrokerSessionMirrorSnapshot> = {},
): BrokerSessionMirrorSnapshot {
  const rows = overrides.rows ?? [];
  return {
    as_of_ms: AS_OF_MS,
    gateway_port: 4002,
    observer_status: 'online',
    ghost_detection_status: 'available',
    global_events: [],
    rows,
    summary: summaryForRows(rows),
    degradation_reasons: [],
    ...overrides,
  };
}

export function botSocket(
  overrides: Partial<BrokerSessionRosterRow> = {},
): BrokerSessionRosterRow {
  const row = {
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
    events: [],
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
  } satisfies Omit<BrokerSessionRosterRow, 'attention_items' | 'presentation'> &
    Partial<Pick<BrokerSessionRosterRow, 'attention_items' | 'presentation'>>;
  return {
    ...row,
    attention_items: overrides.attention_items ?? attentionItemsFor(row.attention_codes),
    presentation: overrides.presentation ?? presentationFor(row),
  };
}

export function historyBot(
  strategyInstanceId: string,
  clientId: number,
): BrokerSessionRosterRow {
  return botSocket({
    row_id: `history:${clientId}`,
    strategy_instance_id: strategyInstanceId,
    client_id: clientId,
    recency: 'past_closed',
    socket_present: false,
    attention_codes: [],
  });
}

export function brokerEvent(
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

export function orphanNotice(): BrokerSessionRosterRow['notice'] {
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

export function ghostSocket(): BrokerSessionRosterRow {
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

function summaryForRows(
  rows: readonly BrokerSessionRosterRow[],
): BrokerSessionMirrorSnapshot['summary'] {
  return rows.reduce<BrokerSessionMirrorSnapshot['summary']>(
    (summary, row) => ({
      current: summary.current + (row.recency === 'current' ? 1 : 0),
      past:
        summary.past +
        (row.recency !== 'current' && row.recency !== 'unknown' ? 1 : 0),
      unknown: summary.unknown + (row.recency === 'unknown' ? 1 : 0),
      attention: summary.attention + (row.attention_codes.length > 0 ? 1 : 0),
    }),
    { current: 0, past: 0, unknown: 0, attention: 0 },
  );
}

function attentionItemsFor(
  codes: BrokerSessionRosterRow['attention_codes'],
): BrokerSessionRosterRow['attention_items'] {
  return codes.map((code) => {
    switch (code) {
      case 'REGISTRY_SAYS_OFFLINE_BUT_SOCKET_LIVE':
        return {
          code,
          label: 'Registry offline; socket live',
          severity: 'warning',
          summary: 'The daemon registry says this process is offline, but the socket is still connected.',
        };
      case 'GHOST_SOCKET':
        return {
          code,
          label: 'Unattributed broker socket',
          severity: 'warning',
          summary: 'A broker socket is present but cannot be attributed to a known bot run.',
        };
      case 'CLIENT_SIGNAL_STALE':
        return {
          code,
          label: 'Client signal stale',
          severity: 'warning',
          summary: 'The latest broker runtime signal is older than the mirror freshness window.',
        };
      case 'ORPHANED_BOT_SOCKET':
        return {
          code,
          label: 'Orphaned bot socket',
          severity: 'critical',
          summary: 'A bot-owned broker socket appears to outlive its host process.',
        };
      case 'SOCKET_WITHOUT_LIVE_PID':
        return {
          code,
          label: 'No live PID',
          severity: 'critical',
          summary: 'The gateway socket is known to a bot, but the owning process PID is unavailable.',
        };
      case 'GHOST_DETECTION_UNAVAILABLE':
        return {
          code,
          label: 'Socket attribution unavailable',
          severity: 'warning',
          summary: 'The daemon socket probe is unavailable, so current socket attribution cannot be proven.',
        };
      case 'STARTED_BUT_NO_SOCKET':
      case 'REGISTRY_SNAPSHOT_UNAVAILABLE':
      case 'SOCKET_ATTRIBUTION_UNAVAILABLE':
        return {
          code,
          label: code.replaceAll('_', ' ').toLowerCase(),
          severity: 'warning',
          summary: null,
        };
    }
  });
}

function presentationFor(
  row: Pick<
    BrokerSessionRosterRow,
    'identity_type' | 'recency' | 'connection_state' | 'recovery_state' | 'strategy_instance_id'
  >,
): BrokerSessionRosterRow['presentation'] {
  return {
    display_name: displayNameFor(row),
    identity: row.identity_type === 'ghost'
      ? { label: 'Unattributed broker socket', severity: 'warning' }
      : row.identity_type === 'orphaned_bot_socket'
        ? { label: 'Orphaned bot socket', severity: 'critical' }
        : row.identity_type === 'system'
          ? { label: 'System infrastructure', severity: 'info' }
          : { label: 'Bot session', severity: 'ok' },
    recency: row.recency === 'current'
      ? { label: 'Live now', severity: 'ok' }
      : row.recency === 'unknown'
        ? { label: 'Unproven now', severity: 'warning' }
        : row.recency === 'past_last_known'
          ? { label: 'Last known', severity: 'neutral' }
          : { label: 'Past session', severity: 'neutral' },
    broker: brokerPresentation(row.connection_state),
    recovery: recoveryPresentation(row.recovery_state),
  };
}

function displayNameFor(
  row: Pick<BrokerSessionRosterRow, 'identity_type' | 'strategy_instance_id'>,
): string {
  if (row.strategy_instance_id) return row.strategy_instance_id;
  if (row.identity_type === 'system') return 'Data-plane broker client';
  if (row.identity_type === 'orphaned_bot_socket') return 'Orphaned bot socket';
  if (row.identity_type === 'ghost') return 'Unattributed broker socket';
  return 'Broker session';
}

function brokerPresentation(
  state: BrokerSessionRosterRow['connection_state'],
): BrokerSessionRosterRow['presentation']['broker'] {
  if (state === 'connected') return { label: 'Broker connected', severity: 'ok' };
  if (state === 'hard_down') {
    return { label: 'Broker recovery exhausted', severity: 'critical' };
  }
  if (state === null) return { label: 'Broker state not reported', severity: 'neutral' };
  return { label: state.replaceAll('_', ' '), severity: 'warning' };
}

function recoveryPresentation(
  state: BrokerSessionRosterRow['recovery_state'],
): BrokerSessionRosterRow['presentation']['recovery'] {
  if (state === 'HEALTHY') return { label: 'Healthy', severity: 'ok' };
  if (state === 'HARD_DOWN' || state === 'SOCKET_DOWN') {
    return { label: state === 'HARD_DOWN' ? 'Hard down' : 'Socket down', severity: 'critical' };
  }
  if (state === null) return { label: 'Recovery unknown', severity: 'neutral' };
  return { label: state.replaceAll('_', ' ').toLowerCase(), severity: 'warning' };
}
