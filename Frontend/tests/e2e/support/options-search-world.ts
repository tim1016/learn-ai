import type { Page } from '@playwright/test';

import type { SavedSetup, ServedSetup } from '../../../src/app/components/options-backtest/options-backtest.types';
import type { OptionsStudy, PlanRequest, Preflight, SearchCandidate, SearchDeclarations, SessionResult, StudyCommand } from '../../../src/app/components/options-golden-search/options-golden-search.types';

export const OPTIONS_SEARCH_API = '/api/research/options-golden-search';
export const OPTIONS_SEARCH_ID = 'options-browser-study';
export const OPTIONS_CANDIDATE_HASH = 'c'.repeat(64);
export const OPTION_SETUP: ServedSetup = {
  version: 1, underlying: 'SPY', starting_cash_usd: '10000', square_off: 'complete',
  entry: { anchor: 'open', offset_ms: 300000 }, exit: { anchor: 'close', offset_ms: 2760000 },
  no_reentry_after: { anchor: 'close', offset_ms: 3600000 }, fees: { kind: 'per_contract', per_contract_usd: '0.65' },
  slippage_percent: '0.3', price_step_usd: '0.01',
  legs: [{ leg_id: 'call', side: 'buy', right: 'call', contracts: 1, expiry: { kind: 'this_week' }, strike: { kind: 'atm' }, stop_loss: { kind: 'percent', value: '25' } },
    { leg_id: 'put', side: 'buy', right: 'put', contracts: 1, expiry: { kind: 'this_week' }, strike: { kind: 'atm' }, stop_loss: { kind: 'percent', value: '25' } }],
};
export const SAVED_OPTION_SETUP: SavedSetup = { name: 'Saved two-leg study', setup: OPTION_SETUP, example: false, saved_at_ms: 1791294000000, cannot_open_reason: null };
const DECLARATIONS: SearchDeclarations = {
  setup_name: SAVED_OPTION_SETUP.name, setup: OPTION_SETUP, setup_hash: 'a'.repeat(64),
  history_bounds: { start_ms: 1704171600000, end_ms: 1790999999999 },
  knobs: [{ path: 'legs.call.stop_loss.value', label: 'Call stop loss', leg_id: 'call', current_value: '25', value_type: 'decimal', legal_values: [], min: '0.0001', max: '99.9999', step: '0.0001', help: 'Holding this value keeps the saved stop rule.' }],
  defaults: { version: 'options-starting-limits-v1', rationale: 'Policy hurdles do not establish confidence.', policy: { max_drawdown_percent: '20', min_sharpe: 1, min_activity_per_year: 50, min_daily_observations: 30, min_forward_sessions: 30, min_final_sessions: 30, max_recovery_sessions: null, risk_free_daily_return: 0, stress_gate: false }, train_months: 6, test_months: 2, final_months: 6, evaluation_cap: 5000, finalist_count: 3 },
};
const PREFLIGHT: Preflight = {
  preflight_hash: 'b'.repeat(64), can_lock: true, blockers: [], warnings: ['Minute-close marks cannot establish tick-level worst loss.'],
  drawdown_limit_usd: '2000', combination_count: 2, legal_combination_count: 2, invalid_combination_count: 0,
  required_evaluations: 30, reserved_evaluations: 20, estimated_setup_sessions: 3000,
  mark_coverage: 'Fixture coverage admitted; individual paths still require verification.', exposure: 'No recorded final exposure in this fixture.', identity: 'frozen-browser-fixture',
  windows: [{ label: 'Development', start_ms: 1704171600000, end_ms: 1773806400000, scheduled_sessions: 550, required_traded_sessions: 100 }, { label: 'Final', start_ms: 1773806400000, end_ms: 1789790400000, scheduled_sessions: 128, required_traded_sessions: 30 }],
};
export const OPTION_SESSION: SessionResult = { session_open_ms: 1789738200000, net_profit_usd: '19.95', status: 'traded', detail: 'One completed setup session.', attempts: [{ leg_id: 'call', attempt: 1, contract: 'O:SPY260918C00600000', side: 'buy', contracts: 1, entry_ms: 1789738500000, exit_ms: 1789757400000, entry_price_usd: '2.50', exit_price_usd: '2.7125', entry_fee_usd: '0.65000000000000000001', exit_fee_usd: '0.64999999999999999999', net_profit_usd: '19.95', exit_reason: 'scheduled_exit' }] };
function candidate(original = false, changes: Partial<SearchCandidate> = {}): SearchCandidate {
  return { candidate_hash: original ? 'e'.repeat(64) : OPTIONS_CANDIDATE_HASH, label: original ? 'Original saved setup' : 'Candidate one', is_original: original, verdict: 'meets_rules', net_profit_usd: original ? '100.01' : '200.01234567890123456789', drawdown_usd: '100', drawdown_fraction: '0.01', drawdown_percent: '1', sharpe: 1.2, completed_sessions: 40, recovery: 'Longest underwater spell: 5 sessions', gate_outcomes: [{ gate_id: 'net_profit', status: 'meets_rules', actual: original ? '100.01' : '200.01234567890123456789', required: '>0', reason: 'Meets the frozen positive-net rule.' }], evidence: [{ label: 'Forward evidence', value: 'Complete reset-capital forward coverage; three folds.' }], monthly: [{ month_ms: 1789738200000, net_profit_usd: '19.95', completed_sessions: 1 }], equity: [{ timestamp_ms: 1789738500000, equity_usd: '10000', drawdown_usd: '0' }, { timestamp_ms: 1789757400000, equity_usd: '10019.95', drawdown_usd: '0' }], sessions: [{ ...OPTION_SESSION, attempts: [] }], ...changes };
}
export function lockedOptionsStudy(changes: Partial<OptionsStudy> = {}): OptionsStudy {
  return { id: OPTIONS_SEARCH_ID, revision: 1, phase: 'locked', status: 'ready', protocol_hash: 'd'.repeat(64), protocol_summary: [{ label: 'Starting capital', value: '10000 USD' }, { label: 'Search dimension', value: 'Call stop loss: 20, 25; order 5' }], setup_name: SAVED_OPTION_SETUP.name, underlying: 'SPY', progress: { stage: 'locked', completed: 0, queued: 0, running: 0, cache_hits: 0, execution_attempts: 0, spent: 0, reserved: 20, capacity_message: 'Width one; capacity qualification remains separate.' }, candidates: [], final_results: [], gate_outcomes: [], exposure: { classification: 'not_opened_in_recorded_research', explanation: 'Recorded study exposure only; outside research cannot be observed.' }, allowed_commands: ['run_research'], dispatch: null, failure_reason: null, qualification_id: null, qualification_status: null, ...changes };
}
export function exceptionOptionsStudy(technical = false): OptionsStudy {
  const failed = candidate(false, { verdict: technical ? 'could_not_evaluate' : 'does_not_meet_rules', sharpe: technical ? null : 0.4, gate_outcomes: [{ gate_id: technical ? 'mark_coverage' : 'sharpe', status: technical ? 'could_not_evaluate' : 'does_not_meet_rules', actual: technical ? 'Missing contract mark' : '0.4', required: technical ? 'Complete contemporaneous marks' : '1.0', reason: technical ? 'Risk path cannot be verified.' : 'The final Sharpe floor was not met.' }] });
  return lockedOptionsStudy({ revision: 7, phase: 'approval', candidates: [candidate(true), candidate()], final_results: [failed], allowed_commands: technical ? ['keep_original'] : ['approve_exception', 'keep_original'], exposure: { classification: 'previously_used', explanation: 'This interval was used in another recorded Options study.' } });
}

/** Every study response is a browser fixture; no production service receives a request. */
export async function installOptionsSearchWorld(page: Page, initial: OptionsStudy | null = null) {
  let current = initial;
  const preflights: PlanRequest[] = [];
  const locks: { request: PlanRequest; preflight_hash: string; idempotency_key: string }[] = [];
  const commands: StudyCommand[] = [];
  const sessionReads: string[] = [];
  const jobStages: string[] = [];
  const unexpected: string[] = [];
  await page.route('**/api/**', async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const fulfill = (json: unknown, status = 200) => route.fulfill({ status, json, headers: { 'Access-Control-Allow-Origin': '*' } });
    if (path === '/api/research/options-backtest/setups') await fulfill([SAVED_OPTION_SETUP]);
    else if (path === '/api/calendar/sessions') {
      // Pinned fixture sessions containing the dates typed in this browser test.
      await fulfill({ session_open_ms: [1704205800000, 1789738200000], session_close_ms: [1704229200000, 1789761600000], holidays: [] });
    } else if (path === `${OPTIONS_SEARCH_API}/declarations`) await fulfill(DECLARATIONS);
    else if (path === `${OPTIONS_SEARCH_API}/preflight`) { preflights.push(request.postDataJSON()); await fulfill(PREFLIGHT); }
    else if (path === `${OPTIONS_SEARCH_API}/studies` && request.method() === 'POST') {
      locks.push(request.postDataJSON()); current = lockedOptionsStudy(); await fulfill(current);
    } else if (path === `${OPTIONS_SEARCH_API}/studies`) {
      await fulfill(current === null ? [] : [{ id: current.id, setup_name: current.setup_name, underlying: current.underlying, phase: current.phase, status: current.status, updated_at_ms: 1791294000000 }]);
    } else if (path === `${OPTIONS_SEARCH_API}/studies/${OPTIONS_SEARCH_ID}` && current !== null) await fulfill(current);
    else if (path === `${OPTIONS_SEARCH_API}/studies/${OPTIONS_SEARCH_ID}/commands` && current !== null) {
      const body: StudyCommand = request.postDataJSON();
      commands.push(body);
      if (body.expected_revision !== current.revision) { await fulfill({ detail: 'Stale revision' }, 409); return; }
      if (body.command === 'run_research') current = { ...current, revision: 2, phase: 'research', status: 'running', allowed_commands: ['cancel'], dispatch: { study_id: current.id, stage_token: 'research-stage' }, progress: { ...current.progress, stage: 'research', running: 1 } };
      else if (body.command === 'open_final') current = { ...current, revision: 4, phase: 'final', status: 'running', allowed_commands: ['cancel'], dispatch: { study_id: current.id, stage_token: 'final-stage' }, progress: { ...current.progress, stage: 'final', running: 1 } };
      else if (body.command === 'approve' || body.command === 'approve_exception') current = { ...current, revision: current.revision + 1, phase: 'approved', status: 'ready', allowed_commands: [], dispatch: null, qualification_id: 'ogs_browser_approved', qualification_status: 'ready' };
      await fulfill(current);
    } else if (path === '/api/jobs/options_golden_search' && current !== null) {
      const token: string = request.postDataJSON().stage_token;
      jobStages.push(token);
      current = token === 'research-stage' ? { ...current, revision: 3, phase: 'compare', status: 'ready', allowed_commands: ['open_final', 'keep_original'], candidates: [candidate(true), candidate()], dispatch: null, progress: { ...current.progress, completed: 10, running: 0, spent: 10 } } : { ...current, revision: 5, phase: 'approval', status: 'ready', allowed_commands: ['approve', 'approve_exception', 'keep_original'], final_results: [candidate(true), candidate()], dispatch: null, progress: { ...current.progress, completed: 14, running: 0, spent: 14 } };
      await fulfill({ id: `browser-${token}`, status: 'queued' });
    } else if (path.startsWith('/api/jobs/') && path.endsWith('/events')) await route.fulfill({ contentType: 'text/event-stream', body: 'event: job.completed\ndata: {"type":"job.completed"}\n\n' });
    else if (path === '/api/jobs') await fulfill([]);
    else if (path.includes(`/studies/${OPTIONS_SEARCH_ID}/candidates/`) && path.includes('/sessions/')) {
      sessionReads.push(`${path}?${url.searchParams}`); await fulfill(OPTION_SESSION);
    } else {
      if (path.startsWith(OPTIONS_SEARCH_API)) unexpected.push(`${request.method()} ${path}`);
      await fulfill({ detail: 'Outside this isolated browser fixture.' }, 503);
    }
  });
  return { preflights, locks, commands, sessionReads, jobStages, unexpected };
}
