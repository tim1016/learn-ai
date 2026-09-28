import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import type { LaneVerdictState } from '../services/alpaca-live-verdict.service';

/** One server-shaped verdict for any final verdict value. The fields are
 * inert fixture values; `final_verdict` varies because that is the field
 * every consumer branches on, and `overrides` names the few others a
 * particular consumer reads (readiness, shadow authority). */
export function fakeAlpacaLiveVerdict(
  finalVerdict: AlpacaLiveVerdict['final_verdict'],
  overrides: Partial<AlpacaLiveVerdict> = {},
): AlpacaLiveVerdict {
  return {
    configured_mode: finalVerdict === 'paper' ? 'paper' : 'live',
    observed_account_id: null,
    mode_agreement: 'agreed',
    clerk_authority: finalVerdict === 'shadow' ? 'shadow' : 'sqlite',
    clerk_refusal_reason_code: null,
    budget_authority_version: 2,
    deployment_readiness: 'ready',
    loss_hold: 'not_applicable',
    final_verdict: finalVerdict,
    headline: 'fixture verdict',
    detail: 'fixture detail',
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

/** The service-level state wrapping {@link fakeAlpacaLiveVerdict}: a
 * successful read holding that verdict. */
export function fakeVerdictState(
  finalVerdict: AlpacaLiveVerdict['final_verdict'],
  overrides: Partial<AlpacaLiveVerdict> = {},
): LaneVerdictState {
  return { verdict: fakeAlpacaLiveVerdict(finalVerdict, overrides), lastError: null };
}
