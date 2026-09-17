import { fireEvent, render, screen, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type {
  AlpacaDeskAccountChoice,
  AlpacaDeskState,
  BrokerAccountSnapshot,
} from '../../../api/alpaca.types';
import { provideFleetDirectory, testLane } from '../../../fleet/fleet-directory-testing';
import { BrokersService } from '../../../services/brokers.service';
import { AlpacaDeskAccountStateComponent } from './alpaca-desk-account-state.component';

/** The directory the strip names an *observed* account from when no
 * configuration choice is effective. One lane, bound to `accountId`, named
 * `displayLabel` — the same two facts `laneDisplayName` resolves a name
 * from anywhere else in the app. */
function directory(accountId = 'PA-123', displayLabel = 'Paper') {
  const lane = testLane({
    clerk_id: 'clrk_spec',
    display_label: displayLabel,
    provider_summary: { ...testLane().provider_summary, confirmed_account_id: accountId },
  });
  return provideFleetDirectory({ observed_at_ms: 1, clerks: [lane] });
}

function snapshot(
  overrides: Partial<BrokerAccountSnapshot> = {},
): BrokerAccountSnapshot {
  return {
    broker: 'alpaca',
    account_id: 'PA-123',
    account_mode: 'paper',
    account_status: 'ACTIVE',
    currency: 'USD',
    cash: 1_000,
    equity: 1_000,
    buying_power: 2_000,
    portfolio_value: 1_000,
    long_market_value: 0,
    short_market_value: 0,
    pattern_day_trader: false,
    trading_blocked: false,
    account_blocked: false,
    created_at_ms: null,
    observed_at_ms: 1,
    ...overrides,
  };
}

function choice(
  overrides: Partial<AlpacaDeskAccountChoice> = {},
): AlpacaDeskAccountChoice {
  return {
    selection_id: 'paper-profile:3',
    profile_id: 'paper-profile',
    revision: 3,
    profile_label: 'Alpaca Paper',
    account_id: 'PA-123',
    nickname: 'Strategy lab',
    // The backend composes this as `nickname or profile.display_name`
    // (`broker_configuration/desk_state.py`) — a friendly name, never the
    // account number. A double that embedded the number here would let an
    // "this strip never shows the number" assertion fail for a fabricated
    // reason.
    account_label: 'Strategy lab',
    endpoint_mode: 'paper',
    badge_label: 'Paper',
    description: 'For strategy testing with no live capital.',
    is_staged: false,
    is_effective: true,
    action_kind: 'review_configuration',
    action_label: 'Review Alpaca Paper',
    action_consequence: 'Review this saved revision without changing the running worker.',
    ...overrides,
  };
}

function state(overrides: Partial<AlpacaDeskState> = {}): AlpacaDeskState {
  const effective = choice();
  return {
    activation_state: 'effective_selection',
    headline: 'Strategy lab is the effective selection',
    detail: 'The worker last acknowledged Alpaca Paper.',
    lifecycle: [
      {
        key: 'effective_configuration',
        label: 'Alpaca Paper is active',
        status: 'complete',
        status_label: 'Complete',
      },
    ],
    selection_label: 'Approved accounts',
    empty_choices_message: null,
    consequence: 'The account could not be reached; no trading controls are available.',
    action: {
      kind: 'review_configuration',
      label: 'Review account configuration',
      enabled: true,
    },
    selection_generation: 9,
    staged_choice: effective,
    effective_choice: effective,
    choices: [effective],
    profiles_requiring_setup: 0,
    setup_required_message: null,
    restart_command: null,
    ...overrides,
  };
}

function brokersService(): Pick<BrokersService, 'getAccount'> {
  return {
    getAccount: vi.fn(() => new Promise<never>(() => undefined)),
  };
}

describe('AlpacaDeskAccountStateComponent', () => {
  it('routes a failed connection to the staged revision named by the action', async () => {
    const effective = choice();
    const staged = choice({
      selection_id: 'live-profile:2',
      profile_id: 'live-profile',
      revision: 2,
      profile_label: 'Alpaca Live',
      account_id: 'LIVE-456',
      account_label: 'LIVE-456',
      endpoint_mode: 'live',
      badge_label: 'Live',
      is_staged: true,
      is_effective: false,
      action_label: 'Review Alpaca Live',
    });
    const reviewRequested = vi.fn();
    const current = state({
      activation_state: 'staged_not_applied',
      effective_choice: effective,
      staged_choice: staged,
      action: {
        kind: 'review_staged_configuration',
        label: 'Review & apply Alpaca Live',
        enabled: true,
      },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: { state: current, accountAvailable: false, accountFailed: true },
      on: { reviewRequested },
      providers: [directory(), { provide: BrokersService, useValue: brokersService() }],
    });

    await fireEvent.click(screen.getByRole('button', { name: current.action.label }));

    expect(reviewRequested).toHaveBeenCalledWith(staged);
    expect(reviewRequested).not.toHaveBeenCalledWith(effective);
  });

  it('separates the effective account from a pending restart when connectivity fails', async () => {
    const effective = choice();
    const staged = choice({
      selection_id: 'live-profile:2',
      profile_id: 'live-profile',
      revision: 2,
      profile_label: 'Alpaca Live',
      account_id: 'LIVE-456',
      account_label: 'LIVE-456',
      endpoint_mode: 'live',
      badge_label: 'Live',
      is_staged: true,
      is_effective: false,
    });
    const current = state({
      activation_state: 'apply_requested_restart_required',
      headline: 'Alpaca Live is ready for the next controlled restart',
      detail: 'The current worker still owns Alpaca Paper.',
      consequence: 'Restart this installation before Alpaca Live can become effective.',
      effective_choice: effective,
      staged_choice: staged,
      action: { kind: 'view_restart_steps', label: 'View restart steps', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: { state: current, accountAvailable: false, accountFailed: true },
      providers: [directory(), { provide: BrokersService, useValue: brokersService() }],
    });

    const effectiveSection = screen.getByText('Effective configuration').closest('section');
    if (effectiveSection === null) throw new Error('effective configuration section missing');
    expect(within(effectiveSection).getByText(effective.profile_label)).toBeTruthy();
    expect(within(effectiveSection).getByText(/Strategy lab.*Paper.*Revision 3/)).toBeTruthy();
    expect(within(effectiveSection).queryByText(current.headline)).toBeNull();

    const pendingSection = screen.getByText('Configuration change pending').closest('section');
    if (pendingSection === null) throw new Error('pending configuration section missing');
    expect(within(pendingSection).getByText(current.headline)).toBeTruthy();
    expect(within(pendingSection).getByText(current.consequence)).toBeTruthy();
    expect(screen.getAllByRole('button', { name: current.action.label })).toHaveLength(1);
  });

  it('keeps a connected account visible with the full backend-authored pending warning', async () => {
    const staged = choice({
      selection_id: 'live-profile:2',
      profile_id: 'live-profile',
      revision: 2,
      profile_label: 'Alpaca Live',
      endpoint_mode: 'live',
      badge_label: 'Live',
      is_staged: true,
      is_effective: false,
    });
    const current = state({
      activation_state: 'apply_requested_restart_required',
      headline: 'Alpaca Live is selected, but not effective for this installation',
      detail: 'The current worker still owns Alpaca Paper.',
      consequence: 'Restart this installation before Alpaca Live can become effective.',
      staged_choice: staged,
      action: { kind: 'view_restart_steps', label: 'View restart steps', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: { state: current, accountAvailable: true, accountFailed: false },
      providers: [directory(), { provide: BrokersService, useValue: brokersService() }],
    });

    expect(screen.getByText(current.headline)).toBeTruthy();
    expect(screen.getByText(current.detail)).toBeTruthy();
    expect(screen.getByText(current.consequence)).toBeTruthy();
    expect(screen.getByRole('button', { name: current.action.label })).toBeTruthy();
  });

  it('shows a pending first selection when connectivity succeeds before an effective selection exists', async () => {
    const staged = choice({
      is_staged: true,
      is_effective: false,
      action_kind: 'view_restart_steps',
      action_label: 'View restart steps',
    });
    const current = state({
      activation_state: 'apply_requested_restart_required',
      headline: 'Strategy lab is ready for a controlled restart',
      detail: 'Apply is recorded for this installation.',
      consequence: 'The running worker has not changed.',
      effective_choice: null,
      staged_choice: staged,
      choices: [staged],
      action: { kind: 'view_restart_steps', label: 'View restart steps', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: { state: current, accountAvailable: true, accountFailed: false },
      providers: [directory(), { provide: BrokersService, useValue: brokersService() }],
    });

    expect(screen.getByText(current.headline)).toBeTruthy();
    expect(screen.getByText(current.detail)).toBeTruthy();
    expect(screen.getByText(current.consequence)).toBeTruthy();
    expect(screen.getByRole('button', { name: current.action.label })).toBeTruthy();
  });

  it('honors a backend-disabled pending action and emits nothing', async () => {
    const reviewRequested = vi.fn();
    const current = state({
      activation_state: 'staged_not_applied',
      headline: 'A configuration change is pending',
      staged_choice: choice({ is_staged: true, is_effective: false }),
      action: { kind: 'review_staged_configuration', label: 'Review pending change', enabled: false },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: { state: current, accountAvailable: true, accountFailed: false },
      on: { reviewRequested },
      providers: [directory(), { provide: BrokersService, useValue: brokersService() }],
    });

    const action = screen.getByRole('button', { name: current.action.label });
    expect(action).toHaveProperty('disabled', true);
    await fireEvent.click(action);
    expect(reviewRequested).not.toHaveBeenCalled();
  });

  it('shows the effective identity from effective_choice, never from staged state', async () => {
    const effective = choice();
    const staged = choice({
      profile_id: 'live-profile',
      revision: 2,
      profile_label: 'Alpaca Live',
      endpoint_mode: 'live',
      badge_label: 'Live',
      is_staged: true,
      is_effective: false,
    });
    const current = state({
      activation_state: 'staged_not_applied',
      effective_choice: effective,
      staged_choice: staged,
      action: { kind: 'review_staged_configuration', label: 'Review & apply Alpaca Live', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: {
        state: current,
        accountAvailable: true,
        accountFailed: false,
        snapshot: snapshot(),
      },
      providers: [directory(), { provide: BrokersService, useValue: brokersService() }],
    });

    const strip = screen.getByLabelText('Effective broker identity');
    expect(within(strip).getByText(effective.profile_label)).toBeTruthy();
    expect(within(strip).getByText('Revision 3')).toBeTruthy();
    expect(within(strip).getByText(effective.account_label)).toBeTruthy();
    expect(within(strip).getByText('Paper')).toBeTruthy();
    expect(within(strip).queryByText('Alpaca Live')).toBeNull();
    expect(within(strip).queryByText('Live')).toBeNull();
  });

  it('falls back to the observed snapshot without fabricating a revision', async () => {
    const current = state({
      activation_state: 'staged_not_applied',
      effective_choice: null,
      staged_choice: choice({ is_staged: true, is_effective: false }),
      action: { kind: 'review_staged_configuration', label: 'Review pending change', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: {
        state: current,
        accountAvailable: true,
        accountFailed: false,
        snapshot: snapshot({ account_mode: 'live', account_id: 'LIVE-9' }),
      },
      // Not labelled "Live": that is what the endpoint-mode chip beside it
      // says, and a lane named the same would make the assertion below
      // ambiguous about which of the two it matched.
      providers: [
        directory('LIVE-9', 'Retirement'),
        { provide: BrokersService, useValue: brokersService() },
      ],
    });

    const strip = screen.getByLabelText('Effective broker identity');
    expect(within(strip).getByText('Live')).toBeTruthy();
    expect(within(strip).queryByText(/^Revision/)).toBeNull();
    expect(within(strip).getByText(/no revision yet/)).toBeTruthy();
  });

  // The `effective_choice` branch has always named this line with a friendly
  // backend-composed label (`nickname or display_name`). The fallback used to
  // put the raw account number on the same line — one line, two meanings.
  it("names an observed account by its lane's display name, never its number (#2188)", async () => {
    const current = state({
      activation_state: 'staged_not_applied',
      effective_choice: null,
      staged_choice: choice({ is_staged: true, is_effective: false }),
      action: { kind: 'review_staged_configuration', label: 'Review pending change', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: {
        state: current,
        accountAvailable: true,
        accountFailed: false,
        snapshot: snapshot({ account_mode: 'live', account_id: 'LIVE-9' }),
      },
      providers: [
        directory('LIVE-9', 'Retirement'),
        { provide: BrokersService, useValue: brokersService() },
      ],
    });

    const strip = screen.getByLabelText('Effective broker identity');
    expect(within(strip).getByText('Retirement')).toBeTruthy();
    expect(within(strip).queryByText('LIVE-9')).toBeNull();
  });

  it('omits the account name rather than falling back to the number when no lane serves it', async () => {
    const current = state({
      activation_state: 'staged_not_applied',
      effective_choice: null,
      staged_choice: choice({ is_staged: true, is_effective: false }),
      action: { kind: 'review_staged_configuration', label: 'Review pending change', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: {
        state: current,
        accountAvailable: true,
        accountFailed: false,
        snapshot: snapshot({ account_mode: 'live', account_id: 'LIVE-9' }),
      },
      // A directory that knows some other account: nothing can name LIVE-9.
      providers: [
        directory('PA-OTHER', 'Paper'),
        { provide: BrokersService, useValue: brokersService() },
      ],
    });

    const strip = screen.getByLabelText('Effective broker identity');
    expect(within(strip).getByText('Unconfigured worker account')).toBeTruthy();
    expect(within(strip).queryByText('LIVE-9')).toBeNull();
  });

  it('warns and gates identity-dependent actions when account ids disagree', async () => {
    const reviewRequested = vi.fn();
    const effective = choice({ account_id: 'PA-DECLARED' });
    const current = state({
      activation_state: 'staged_not_applied',
      effective_choice: effective,
      staged_choice: choice({ is_staged: true, is_effective: false }),
      action: { kind: 'review_staged_configuration', label: 'Review pending change', enabled: true },
    });
    await render(AlpacaDeskAccountStateComponent, {
      inputs: {
        state: current,
        accountAvailable: true,
        accountFailed: false,
        snapshot: snapshot({ account_id: 'PA-OBSERVED' }),
      },
      on: { reviewRequested },
      providers: [directory(), { provide: BrokersService, useValue: brokersService() }],
    });

    const warning = screen.getByRole('alert');
    expect(warning.textContent).toContain('PA-DECLARED');
    expect(warning.textContent).toContain('PA-OBSERVED');

    const action = screen.getByRole('button', { name: current.action.label });
    expect(action).toHaveProperty('disabled', true);
    await fireEvent.click(action);
    expect(reviewRequested).not.toHaveBeenCalled();
  });
});
