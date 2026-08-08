import campaignContract from '@repo-contracts/alpaca-clerk-ui-correlation-campaign.v4.json';

import type {
  BotPanelLiveSnapshot,
  BotPanelView,
  EvidencePage,
  PanelProfile,
} from '../../src/app/components/broker/v2-panel/lib/broker-v2-panel.types';

interface UiCorrelationCampaignV4 {
  readonly schema_version: 4;
  readonly campaign_id: string;
  readonly component_campaign_id: string;
  readonly command: readonly string[];
  readonly timeout_seconds: number;
  readonly max_receipt_bytes: number;
  readonly max_evidence_refs: number;
  readonly page_load_count: number;
  readonly browser_reload_count: number;
  readonly revision_sequence: readonly number[];
  readonly bootstrap_revision: number;
  readonly historical_snapshot_state: string;
  readonly presented_action_id: 'resume';
  readonly evidence_click_target: string;
  readonly evidence_button_name: string;
  readonly hide_evidence_button_name: string;
  readonly station_button_name: string;
  readonly account_id: string;
  readonly strategy_instance_id: string;
  readonly transaction_ref: string;
  readonly symbol: string;
  readonly base_timestamp_ms: number;
}

export const UI_CORRELATION_CAMPAIGN = campaignContract as UiCorrelationCampaignV4;
export const ACCOUNT_ID = UI_CORRELATION_CAMPAIGN.account_id;
export const STRATEGY_INSTANCE_ID = UI_CORRELATION_CAMPAIGN.strategy_instance_id;
export const TRANSACTION_REF = UI_CORRELATION_CAMPAIGN.transaction_ref;
export const PAGE_LOAD_COUNT = UI_CORRELATION_CAMPAIGN.page_load_count;
export const BROWSER_RELOAD_COUNT = UI_CORRELATION_CAMPAIGN.browser_reload_count;
export const REVISION_SEQUENCE = UI_CORRELATION_CAMPAIGN.revision_sequence;
export const PRESENTED_ACTION_ID = UI_CORRELATION_CAMPAIGN.presented_action_id;
export const HISTORICAL_SNAPSHOT_STATE =
  UI_CORRELATION_CAMPAIGN.historical_snapshot_state;
export const EVIDENCE_BUTTON_NAME = UI_CORRELATION_CAMPAIGN.evidence_button_name;
export const HIDE_EVIDENCE_BUTTON_NAME =
  UI_CORRELATION_CAMPAIGN.hide_evidence_button_name;
export const STATION_BUTTON_NAME = UI_CORRELATION_CAMPAIGN.station_button_name;
export const EVIDENCE_CLICK_TARGET = UI_CORRELATION_CAMPAIGN.evidence_click_target;
export const EXPECTED_OBSERVATION_COUNT =
  PAGE_LOAD_COUNT * REVISION_SEQUENCE.length;
export const BOOTSTRAP_REVISION = UI_CORRELATION_CAMPAIGN.bootstrap_revision;
export const INITIAL_REVISION = requiredRevision(0);
export const FINAL_REVISION = requiredRevision(REVISION_SEQUENCE.length - 1);

const BASE_TIMESTAMP_MS = UI_CORRELATION_CAMPAIGN.base_timestamp_ms;

function requiredRevision(index: number): number {
  const revision = REVISION_SEQUENCE[index];
  if (revision === undefined) {
    throw new Error(`UI correlation contract is missing revision ${index}.`);
  }
  return revision;
}

export const PROFILE: PanelProfile = {
  broker: 'alpaca',
  fee_fidelity: 'none',
  flatten_supported: false,
  live_bars_supported: false,
  stations: [],
  supported_action_ids: [PRESENTED_ACTION_ID],
};

export function panelAtRevision(revision: number): BotPanelView {
  return {
    strategy_instance_id: STRATEGY_INSTANCE_ID,
    strategy_key: 'ema_crossover',
    strategy_label: 'Ema Crossover',
    broker: 'alpaca',
    account_id: ACCOUNT_ID,
    symbol: UI_CORRELATION_CAMPAIGN.symbol,
    mode: 'log_only',
    updated_at_ms: BASE_TIMESTAMP_MS + revision,
    revision,
    market_pulse: {
      session: 'OPEN',
      feed_state: 'LIVE',
      latest_bar_at_ms: BASE_TIMESTAMP_MS,
      age_ms: 1_000,
      source: 'alpaca',
      expected_cadence_ms: 60_000,
      headline: 'Market data live',
      explanation: 'The feed is current.',
      next_step: null,
      attention_required: false,
      observed_at_ms: BASE_TIMESTAMP_MS + 1_000,
    },
    mission_verdict: {
      state: 'off_duty',
      label: 'Off duty',
      explanation: `Revision ${revision} is stopped after restart.`,
      next_action: 'Resume only after reviewing custody evidence.',
      evaluated_at_ms: BASE_TIMESTAMP_MS + revision,
    },
    execution_policy: 'Observation only.',
    health: {
      strategy_instance_id: STRATEGY_INSTANCE_ID,
      phase: 'OFF_DUTY',
      phase_label: 'Off duty',
      desired_state: 'STOPPED',
      desired_state_label: `Revision ${revision} stopped`,
      running: false,
      duty_outcome: null,
      last_decision_at_ms: null,
      decision_stale: false,
      last_bar_at_ms: null,
      resume_eligible: true,
      resume_label: 'Resume',
      resume_explanation: 'Custody is flat and the stopped bot is eligible to resume.',
      carryover_checkpoint_exposure: {},
    },
    clerk: {
      account_id: ACCOUNT_ID,
      hold_active: false,
      hold_reason: 'NO_HOLD',
      hold_reason_label: 'No hold',
      hold_reason_explanation: 'No hold active.',
      hold_since_ms: null,
      freeze_active: false,
      freeze_category: null,
      freeze_label: 'No account freeze',
      freeze_explanation: 'Account truth is current.',
      freeze_next_step: null,
      freeze_observed_at_ms: null,
      reconciliation_verdict: null,
      reconciliation_verdict_label: null,
      last_sweep_at_ms: null,
      outstanding_intents: 0,
      channels: [],
    },
    rail: {
      transaction_ref: TRANSACTION_REF,
      stations: [{
        station_id: 'BROKER_ACK',
        label: 'Broker ack',
        state: 'satisfied',
        state_label: 'Satisfied',
        receipt: 'The broker accepted the order.',
        evidence_at_ms: BASE_TIMESTAMP_MS,
        blocker: null,
      }],
    },
    journal_tail_ref:
      `/api/brokers/alpaca/accounts/${ACCOUNT_ID}/bots/${STRATEGY_INSTANCE_ID}/journal`,
    journal_tail_seq: null,
    actions: [{
      action_id: PRESENTED_ACTION_ID,
      label: 'Resume',
      explanation: 'Resume evaluating bars after the durable SQLite command.',
      enabled: true,
      blockers: [],
      confirmation: null,
      revision,
      concurrency_token: `resume-token-${revision}`,
    }],
    readiness_checks: [],
    readiness_ready_count: 0,
    readiness_blocked_count: 0,
    exposure: {},
    working_orders: [],
    recent_decisions: [],
    recent_fills: [],
    fills_today: 0,
    realized_pnl_today: 0,
    open_pnl: null,
  };
}

export function snapshotAtRevision(
  pageLoad: number,
  revision: number,
): BotPanelLiveSnapshot {
  return {
    stream_epoch: `browser-reload-${pageLoad}`,
    surface_version: revision,
    panel: panelAtRevision(revision),
    live_chart: {
      strategy_instance_id: STRATEGY_INSTANCE_ID,
      symbol: UI_CORRELATION_CAMPAIGN.symbol,
      trading_date_open_ms: BASE_TIMESTAMP_MS,
      trading_date_close_ms: BASE_TIMESTAMP_MS + 23_400_000,
      resolution: '5s',
      bars: [],
      fill_markers: [],
      overlay_notices: [],
      as_of_ms: BASE_TIMESTAMP_MS + revision,
    },
  };
}

export function evidenceReceipt(
  pageLoad: number,
  eventIndex: number,
  revision: number,
): EvidencePage {
  const receiptRef = correlationReceiptRef(pageLoad, eventIndex, revision);
  return {
    strategy_instance_id: STRATEGY_INSTANCE_ID,
    account_id: ACCOUNT_ID,
    transaction_ref: TRANSACTION_REF,
    entries: [{
      seq: pageLoad * 100 + revision,
      kind: 'submit_acked',
      kind_label: 'Submit acknowledged',
      recorded_at_ms: BASE_TIMESTAMP_MS + 2_000 + revision,
      operation_ref: receiptRef,
      operation_state: 'completed',
      broker_state: 'accepted',
      custody_owner: 'CLERK',
      proof_reference: receiptRef,
      source_event_at_ms: BASE_TIMESTAMP_MS + revision,
      clerk_observed_at_ms: BASE_TIMESTAMP_MS + 1_000 + revision,
      order_ref: `order-${pageLoad}-${revision}`,
      intent_id: `intent-${pageLoad}-${revision}`,
      summary: `Revision ${revision} broker acknowledgement is durable.`,
      has_more_detail: false,
    }],
    next_cursor: null,
    total_entries: 1,
    truncated: false,
    read_by: 'operator:playwright-1415',
    read_at_ms: BASE_TIMESTAMP_MS + 3_000 + revision,
  };
}

export function correlationReceiptRef(
  pageLoad: number,
  eventIndex: number,
  revision: number,
): string {
  return `durable-ui-receipt-${pageLoad}-${eventIndex}-${revision}`;
}
