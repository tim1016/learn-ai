import type { GoldenOptionSetup } from '../golden-option-setups.service';
import { exampleBody, readBackOf } from './fixtures';

export function goldenSetup(changes: Partial<GoldenOptionSetup> = {}): GoldenOptionSetup {
  return {
    id: 'ogs_0123456789abcdef0123456789abcdef', name: 'Tested SPY setup', underlying: 'SPY',
    setup: readBackOf(exampleBody()), setup_sha256: 'a'.repeat(64), source_run_id: 7,
    rules_digest: 'c'.repeat(64), proof_sha256: 'b'.repeat(64), status: 'ready',
    status_explanation: 'This tested setup is approved for the current rules.',
    created_at_ms: 1791828000000, approved_by: 'owner', note: 'Reviewed the exact run.', proof: {}, events: [],
    evidence: {
      first_session_open_ms: 1791379800000, last_session_open_ms: 1791552600000,
      sessions_tested: 3, trades: 4, net_profit_usd: '25.00', starting_cash_usd: '10000', slippage_percent: '0.3',
      limitations: ['Trade bars do not validate quote spreads or actual live fills.'],
    }, ...changes,
  };
}
