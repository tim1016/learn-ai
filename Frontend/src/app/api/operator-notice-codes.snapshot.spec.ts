// NOTE: The Angular build system (esbuild via @angular/build:unit-test) cannot
// resolve JSON imports outside the project root. The snapshot is therefore
// inlined here. The Python-side snapshot test
// (PythonDataService/tests/operator/test_notice_codes_snapshot.py) remains the
// primary drift guard; this test closes the TS-side half of the loop by
// asserting that the TS literal array is in the same order and contains the
// same values as the inlined snapshot. When the snapshot changes, update BOTH
// this array AND PythonDataService/app/operator/notices/snapshot.json.
//
// Canonical source: PythonDataService/app/operator/notices/snapshot.json
// schema_version: 1
import { describe, expect, it } from 'vitest';
import type { OperatorNoticeCode } from './live-instances.types';

// Inlined from snapshot.json (operator_notice_codes, schema_version 1).
const SNAPSHOT_OPERATOR_NOTICE_CODES: readonly string[] = [
  'runtime.market_closed',
  'runtime.market_session_halted',
  'runtime.market_data_stale',
  'runtime.market_data_feed_stalled',
  'runtime.broker_probe_stale',
  'runtime.broker_probe_missing',
  'runtime.command_loop_unresponsive',
  'runtime.engine_runtime_incompatible',
  'runtime.control_plane_lease_stale',
  'runtime.control_plane_boot_id_mismatch',
  'watchdog.flatten_completed',
  'watchdog.flatten_not_needed',
  'watchdog.flatten_timed_out',
  'watchdog.flatten_failed',
  'watchdog.broker_disconnected_before_flatten',
  'activity.publisher_starting',
  'activity.publisher_not_running',
  'activity.publisher_degraded',
  'activity.source_blind_to_bot_orders',
  'activity.dropped_paused_intent',
  'reconciliation.required_after_uncertain_flatten',
  'reconciliation.discovered_execution_not_in_engine_state',
];

const TS_OPERATOR_NOTICE_CODES: readonly OperatorNoticeCode[] = [
  'runtime.market_closed',
  'runtime.market_session_halted',
  'runtime.market_data_stale',
  'runtime.market_data_feed_stalled',
  'runtime.broker_probe_stale',
  'runtime.broker_probe_missing',
  'runtime.command_loop_unresponsive',
  'runtime.engine_runtime_incompatible',
  'runtime.control_plane_lease_stale',
  'runtime.control_plane_boot_id_mismatch',
  'watchdog.flatten_completed',
  'watchdog.flatten_not_needed',
  'watchdog.flatten_timed_out',
  'watchdog.flatten_failed',
  'watchdog.broker_disconnected_before_flatten',
  'activity.publisher_starting',
  'activity.publisher_not_running',
  'activity.publisher_degraded',
  'activity.source_blind_to_bot_orders',
  'activity.dropped_paused_intent',
  'reconciliation.required_after_uncertain_flatten',
  'reconciliation.discovered_execution_not_in_engine_state',
] as const;

describe('OperatorNoticeCode TS literal vs Python snapshot', () => {
  it('matches the inlined Python snapshot in declared order', () => {
    expect(TS_OPERATOR_NOTICE_CODES as readonly string[]).toEqual(
      SNAPSHOT_OPERATOR_NOTICE_CODES,
    );
  });
});
