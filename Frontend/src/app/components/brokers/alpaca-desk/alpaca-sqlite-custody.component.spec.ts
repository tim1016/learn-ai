import { HttpErrorResponse } from '@angular/common/http';
import { By } from '@angular/platform-browser';
import { provideRouter } from '@angular/router';
import { fireEvent, render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import type {
  SqliteClerkProjection,
  SqliteRecoveryAction,
  SqliteRecoveryResult,
  SqliteSafeFlattenPlan,
  SqliteTimelinePage,
} from '../../../api/alpaca.types';
import {
  BrokersService,
  type SqliteTimelineQuery,
} from '../../../services/brokers.service';
import { TypedHaltConfirmComponent } from '../../broker/shared/typed-halt-confirm/typed-halt-confirm.component';
import { AlpacaSqliteCustodyComponent } from './alpaca-sqlite-custody.component';
import { provideFleetDirectory } from '../../../fleet/fleet-directory-testing';
import { resourceTarget } from '../../../fleet/resource-target';
import type { LaneFence } from '../../../fleet/lane-fence';

const NOW = 1_700_000_000_000;
const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA1', bindingGeneration: 4, routingEpoch: 1 });
const FENCE: LaneFence = { bindingGeneration: 4, routingEpoch: 1 };

const SAFE_FLATTEN_PLAN: SqliteSafeFlattenPlan = {
  version_token: 'plan-token-17',
  account_id: 'PA1',
  authority_generation: 4,
  db_identity_token: 'db-generation-4',
  control_revision: 17,
  scope: 'ACCOUNT_CLERK',
  strategy_instance_id: null,
  reconciliation_id: 'reconciliation-17',
  prepared_at_ms: NOW,
  expires_at_ms: 4_102_444_800_000,
  legs: [{
    strategy_instance_id: 'spy-bot',
    symbol: 'SPY',
    side: 'sell',
    quantity: 1.25,
    position_updated_at_ms: NOW - 100,
  }],
};

function action(
  overrides: Partial<SqliteRecoveryAction> = {},
): SqliteRecoveryAction {
  return {
    action_id: 'reconcile_now',
    label: 'Reconcile now',
    explanation: 'Compare durable Clerk custody with a fresh Alpaca observation.',
    available: true,
    unavailable_reason_code: null,
    unavailable_reason: null,
    scope: 'ACCOUNT_CLERK',
    freshness: 'not_required',
    evidence: [],
    reduction_plan: null,
    confirmation: null,
    next_step: 'Run the account comparison now.',
    concurrency_token: 'token-17',
    execution_ref: null,
    mutation: true,
    primary: true,
    ...overrides,
  };
}

function projection(
  actions: readonly SqliteRecoveryAction[] = [action()],
): SqliteClerkProjection {
  return {
    account_id: 'PA1',
    strategy_instance_id: null,
    authority_generation: 4,
    db_identity_token: 'db-generation-4',
    authority_health: 'healthy',
    authority_health_reason: null,
    control_revision: 17,
    custody_owner: 'ACCOUNT_CLERK',
    runs: [],
    commands: [],
    operations: [],
    positions: [],
    holds: [],
    uncertainties: [],
    latest_reconciliation: null,
    terminal_receipts: [],
    guidance: {
      headline: 'Account Clerk custody is healthy',
      explanation: 'Durable Clerk state has no unresolved uncertainty.',
      scope: 'ACCOUNT_CLERK',
      impact: 'Normal Clerk-governed controls remain available.',
      custody_owner: 'ACCOUNT_CLERK',
      may_create_exposure: true,
      available_safety_actions: actions.filter((item) => item.available).map((item) => item.label),
      action_required: false,
      next_step: 'No recovery action is required.',
    },
    recovery_actions: [...actions],
    generated_at_ms: NOW,
  };
}

function timelineEntry(sequence: number): SqliteTimelinePage['entries'][number] {
  return {
    sequence,
    operation_ref: `effect:enter:${sequence}`,
    effect_operation_id: `effect:enter:${sequence}`,
    command_id: `command:enter:${sequence}`,
    order_ref: `order:enter:${sequence}`,
    broker_order_id: `alpaca-order-${sequence}`,
    transition_kind: 'ORDER_FILL_OBSERVED',
    operation_state: 'in_progress',
    broker_state: 'accepted',
    custody_owner: 'ACCOUNT_CLERK',
    execution_authority: 'ACCOUNT_CLERK',
    summary_code: 'ORDER_FILL_OBSERVED',
    proof_reference: `proof:${sequence}`,
    source_event_at_ms: NOW - 300,
    clerk_observed_at_ms: NOW - 200,
    recorded_at_ms: NOW - 100,
  };
}

function timeline(
  overrides: Partial<SqliteTimelinePage> = {},
): SqliteTimelinePage {
  return {
    account_id: 'PA1',
    strategy_instance_id: null,
    authority_generation: 4,
    control_revision: 17,
    anchor_sequence: 12,
    total_entries: 1,
    next_cursor: null,
    entries: [timelineEntry(12)],
    ...overrides,
  };
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

async function renderCustody(
  service: Partial<BrokersService>,
  inputs: { readonly timelineQuery?: SqliteTimelineQuery | null; readonly fence?: LaneFence } = {},
) {
  return render(AlpacaSqliteCustodyComponent, {
    inputs: { accountId: 'PA1', target: TARGET, fence: FENCE, ...inputs },
    providers: [
      provideFleetDirectory(),
      provideRouter([]),
      { provide: BrokersService, useValue: service },
    ],
  });
}

describe('AlpacaSqliteCustodyComponent', () => {
  it('fails closed when the SQLite authority is unavailable', async () => {
    const getSqliteClerkProjection = vi.fn().mockRejectedValue(
      new HttpErrorResponse({ status: 409 }),
    );

    await renderCustody({ getSqliteClerkProjection });

    await waitFor(() => expect(getSqliteClerkProjection).toHaveBeenCalledOnce());
    expect(screen.queryByRole('heading', { name: 'Custody and recovery' })).toBeNull();
    expect(screen.getByRole('alert').textContent).toContain(
      'New broker actions remain blocked',
    );
  });

  it('renders backend-authored guidance and all three custody clocks', async () => {
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([
        action({
          action_id: 'open_custody_timeline',
          label: 'Open custody timeline',
          explanation: 'Inspect immutable custody evidence.',
          mutation: false,
        }),
      ])),
      getSqliteClerkTimeline: vi.fn().mockResolvedValue(timeline()),
    });

    expect(await screen.findByText('Durable Clerk state has no unresolved uncertainty.')).toBeTruthy();
    // #2183: "Custody and recovery" carries the eyebrow look itself now; the
    // separate "Order record" label above it is retired.
    expect(screen.getByRole('heading', { name: 'Custody and recovery' })).toBeTruthy();
    expect(screen.queryByText('Order record')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Open custody timeline' }));

    expect((await screen.findAllByText('effect:enter:12')).length).toBeGreaterThan(0);
    expect(screen.getByText('Source event')).toBeTruthy();
    expect(screen.getByText('Clerk observed')).toBeTruthy();
    expect(screen.getByText('Durably recorded')).toBeTruthy();
  });

  it('identifies the affected bot and links directly to its recovery controls', async () => {
    const blocked = projection([]);
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue({
        ...blocked,
        uncertainties: [{
          uncertainty_id: 'uncertainty:17',
          scope: 'CUSTODY_SUBJECT',
          severity: 'error',
          blocks_new_exposure: true,
          allows_reduction: false,
          custody_owner: 'ACCOUNT_CLERK',
          strategy_instance_id: 'spy-bot',
          reason_code: 'EXECUTION_COVERAGE_CONFLICT',
          headline: 'Exact execution conflicts with prior immutable evidence',
          explanation: 'The execution must be recovered from Alpaca activity.',
          operator_impact: 'New exposure remains blocked.',
          next_step: 'Recover exact execution evidence for the affected bot.',
          observed_at_ms: NOW,
          evidence_age_ms: 0,
          evidence_refs: ['execution:17'],
        }],
      }),
    });

    const botLink = await screen.findByRole('link', { name: 'Review spy-bot recovery' });
    expect(botLink.getAttribute('href'))
      .toBe('/brokers/alpaca/accounts/PA1/bots/spy-bot?lens=operator');
    // No retry is scheduled for this cause, so no attempt time is shown.
    expect(screen.queryByText(/Next automatic attempt/)).toBeNull();
  });

  it('says when the Clerk next tries to sell a position an exit left open (#2440)', async () => {
    // 2026-09-03 04:00 ET: the pre-market open after an after-hours exit that
    // could not go out. The backend sends the instant; this page formats it.
    const nextAttemptAtMs = 1_788_422_400_000;
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue({
        ...projection([]),
        uncertainties: [{
          uncertainty_id: 'uncertainty:21',
          scope: 'CUSTODY_SUBJECT',
          severity: 'error',
          blocks_new_exposure: true,
          allows_reduction: true,
          custody_owner: 'ACCOUNT_CLERK',
          strategy_instance_id: 'spy-bot',
          reason_code: 'EXIT_NOT_FLAT',
          headline: 'An exit could not be sent after its session ended; the position is still open',
          explanation: '10 SPY is still held.',
          operator_impact: 'New exposure is paused for this strategy.',
          next_step: 'Flatten with a priced limit now, or let the automatic re-drive reduce it.',
          observed_at_ms: NOW,
          evidence_age_ms: 0,
          evidence_refs: ['order:1'],
          next_attempt_at_ms: nextAttemptAtMs,
        }],
      }),
    });

    const attempt = await screen.findByText(/Next automatic attempt/);
    expect(attempt.textContent).toContain('04:00');
    expect(attempt.textContent).toContain('ET');
  });

  function unfoldableProjection(): SqliteClerkProjection {
    return {
      ...projection([]),
      uncertainties: [{
        uncertainty_id: 'uncertainty:unfoldable',
        scope: 'ACCOUNT_CLERK',
        severity: 'error',
        blocks_new_exposure: true,
        allows_reduction: true,
        custody_owner: 'ACCOUNT_CLERK',
        strategy_instance_id: null,
        reason_code: 'UNFOLDABLE_BROKER_ORDER',
        headline: 'A broker order could not be recorded',
        explanation: 'The Clerk could not record 2 broker order(s).',
        operator_impact: 'New entries are paused account-wide.',
        next_step: 'Inspect each named order at Alpaca, then acknowledge it.',
        observed_at_ms: NOW,
        evidence_age_ms: 0,
        evidence_refs: ['mleg-parent-1', 'mleg-parent-2'],
      }],
    };
  }

  it('acknowledges an order the Clerk could not record on the external-order route', async () => {
    const getSqliteClerkProjection = vi.fn().mockResolvedValue(unfoldableProjection());
    const acknowledgeExternalOrder = vi.fn().mockResolvedValue({
      external_order_id: 'mleg-parent-1',
      acknowledged_at_ms: NOW,
      ack_operator: 'operator-1',
    });
    await renderCustody({ getSqliteClerkProjection, acknowledgeExternalOrder });

    const acknowledge = await screen.findByRole('button', {
      name: 'Acknowledge broker order mleg-parent-1',
    });
    expect((acknowledge as HTMLButtonElement).disabled).toBe(true);
    fireEvent.input(screen.getByLabelText('Reviewed by'), { target: { value: ' operator-1 ' } });
    fireEvent.click(acknowledge);

    await waitFor(() => expect(acknowledgeExternalOrder).toHaveBeenCalledOnce());
    const [target, brokerOrderId, operator] = acknowledgeExternalOrder.mock.calls[0];
    expect(brokerOrderId).toBe('mleg-parent-1');
    expect(operator).toBe('operator-1');
    expect(target).toMatchObject({ clerkId: 'clrk_spec', accountId: 'PA1', bindingGeneration: 4 });
    // Only this order's review is claimed; mleg-parent-2 may still hold the pause.
    expect(await screen.findByText('Broker order mleg-parent-1 was reviewed.')).toBeTruthy();
    expect(screen.queryByText(/released from the entry pause/)).toBeNull();
    await waitFor(() => expect(getSqliteClerkProjection).toHaveBeenCalledTimes(2));
  });

  it('keeps an acknowledgement result on the lane that started it', async () => {
    const pending = deferred<unknown>();
    const acknowledgeExternalOrder = vi.fn().mockReturnValue(pending.promise);
    const getSqliteClerkProjection = vi.fn().mockResolvedValue(unfoldableProjection());
    const view = await renderCustody({ getSqliteClerkProjection, acknowledgeExternalOrder });

    fireEvent.input(await screen.findByLabelText('Reviewed by'), {
      target: { value: 'operator-1' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Acknowledge broker order mleg-parent-1' }));
    await waitFor(() => expect(acknowledgeExternalOrder).toHaveBeenCalledOnce());
    view.fixture.componentRef.setInput('target', resourceTarget('alpaca', 'clrk_other', {
      accountId: 'PA1', bindingGeneration: 5, routingEpoch: 2,
    }));
    view.fixture.detectChanges();
    await waitFor(() => expect(getSqliteClerkProjection).toHaveBeenCalledTimes(2));
    pending.reject(new Error('stale lane refused'));

    // The settled request's refresh is the last thing it does.
    await waitFor(() => expect(getSqliteClerkProjection).toHaveBeenCalledTimes(3));
    expect(screen.queryByText('The Account Clerk could not acknowledge this order.')).toBeNull();
    expect(screen.queryByText('stale lane refused')).toBeNull();
  });

  it('paginates the custody timeline instead of silently truncating past the first page', async () => {
    const getSqliteClerkTimeline = vi.fn()
      .mockResolvedValueOnce(timeline({
        entries: [timelineEntry(12)],
        next_cursor: 'cursor-11',
        total_entries: 2,
      }))
      .mockResolvedValueOnce(timeline({
        entries: [timelineEntry(11)],
        next_cursor: null,
        total_entries: 2,
      }));
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([
        action({
          action_id: 'open_custody_timeline',
          label: 'Open custody timeline',
          explanation: 'Inspect immutable custody evidence.',
          mutation: false,
        }),
      ])),
      getSqliteClerkTimeline,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Open custody timeline' }));
    expect((await screen.findAllByText('effect:enter:12')).length).toBeGreaterThan(0);
    expect(screen.getByText('1 of 2')).toBeTruthy();

    const loadMore = screen.getByRole('button', { name: 'Load more events' });
    fireEvent.click(loadMore);

    expect((await screen.findAllByText('effect:enter:11')).length).toBeGreaterThan(0);
    expect(screen.getAllByText('effect:enter:12').length).toBeGreaterThan(0);
    expect(screen.getByText('2 of 2')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Load more events' })).toBeNull();
    expect(getSqliteClerkTimeline).toHaveBeenNthCalledWith(2, 'clrk_spec', 'PA1', { cursor: 'cursor-11' });
  });

  it('continues a timeline page with the filters that minted its cursor', async () => {
    const getSqliteClerkTimeline = vi.fn()
      .mockResolvedValueOnce(timeline({
        entries: [timelineEntry(12)],
        next_cursor: 'cursor-11',
        total_entries: 2,
      }))
      .mockResolvedValueOnce(timeline({
        entries: [timelineEntry(11)],
        next_cursor: null,
        total_entries: 2,
      }));
    await renderCustody(
      {
        getSqliteClerkProjection: vi.fn().mockResolvedValue(projection()),
        getSqliteClerkTimeline,
      },
      { timelineQuery: { orderRef: 'order:enter:12' } },
    );

    await screen.findAllByText('effect:enter:12');
    fireEvent.input(screen.getByLabelText('Order reference'), {
      target: { value: 'order:edited-before-load-more' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Load more events' }));

    await screen.findAllByText('effect:enter:11');
    expect(getSqliteClerkTimeline).toHaveBeenNthCalledWith(2, 'clrk_spec', 'PA1', {
      orderRef: 'order:enter:12',
      cursor: 'cursor-11',
    });
  });

  it('does not publish a stale load-more failure after the lane changes', async () => {
    const stalePage = deferred<SqliteTimelinePage>();
    const getSqliteClerkTimeline = vi.fn()
      .mockResolvedValueOnce(timeline({ next_cursor: 'cursor-11', total_entries: 2 }))
      .mockReturnValueOnce(stalePage.promise);
    const view = await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([action({
        action_id: 'open_custody_timeline',
        label: 'Open custody timeline',
        mutation: false,
      })])),
      getSqliteClerkTimeline,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Open custody timeline' }));
    const loadMore = await screen.findByRole('button', { name: 'Load more events' });
    fireEvent.click(loadMore);
    await waitFor(() => expect(getSqliteClerkTimeline).toHaveBeenCalledTimes(2));
    view.fixture.componentRef.setInput('target', resourceTarget('alpaca', 'clrk_other', {
      accountId: 'PA1', bindingGeneration: 5, routingEpoch: 2,
    }));
    view.fixture.detectChanges();
    stalePage.reject(new Error('stale lane unavailable'));

    await waitFor(() => expect(screen.queryByText('The custody timeline is temporarily unavailable.')).toBeNull());
    await waitFor(() => expect(
      screen.getByRole('button', { name: 'Load more events' }).hasAttribute('disabled'),
    ).toBe(false));
  });

  it('opens exact deep-link filters and updates immutable evidence on row selection', async () => {
    const getSqliteClerkTimeline = vi.fn().mockResolvedValue(timeline({
      entries: [timelineEntry(12), timelineEntry(11)],
      total_entries: 2,
    }));
    const query: SqliteTimelineQuery = {
      strategyInstanceId: 'spy-bot',
      orderRef: 'order:enter:12',
      uncertaintyId: 'uncertainty:12',
      executionId: 'execution:12',
      transitionKind: 'ORDER_FILL_OBSERVED',
      sequence: 12,
    };
    await renderCustody(
      {
        getSqliteClerkProjection: vi.fn().mockResolvedValue(projection()),
        getSqliteClerkTimeline,
      },
      { timelineQuery: query },
    );

    await waitFor(() => expect(getSqliteClerkTimeline).toHaveBeenCalledWith('clrk_spec', 'PA1', query));
    fireEvent.click(await screen.findByRole('button', { name: /effect:enter:11/i }));

    expect(getSqliteClerkTimeline).toHaveBeenCalledOnce();
    expect(screen.getByLabelText('Selected immutable evidence').textContent).toContain(
      'effect:enter:11',
    );
  });

  it('loads the newest deep-link query after an earlier timeline request is in flight', async () => {
    const firstTimeline = deferred<SqliteTimelinePage>();
    const getSqliteClerkTimeline = vi.fn()
      .mockReturnValueOnce(firstTimeline.promise)
      .mockResolvedValueOnce(timeline({ entries: [timelineEntry(11)] }));
    const view = await renderCustody(
      {
        getSqliteClerkProjection: vi.fn().mockResolvedValue(projection()),
        getSqliteClerkTimeline,
      },
      { timelineQuery: { orderRef: 'order:enter:12' } },
    );

    await waitFor(() => expect(getSqliteClerkTimeline).toHaveBeenCalledOnce());
    view.fixture.componentRef.setInput('timelineQuery', { orderRef: 'order:enter:11' });
    firstTimeline.resolve(timeline());

    await waitFor(() => {
      expect(getSqliteClerkTimeline).toHaveBeenLastCalledWith('clrk_spec', 'PA1', {
        orderRef: 'order:enter:11',
      });
    });
    expect((await screen.findAllByText('effect:enter:11')).length).toBeGreaterThan(0);
  });

  it('continues with the new lane query when the prior timeline request fails', async () => {
    const staleTimeline = deferred<SqliteTimelinePage>();
    const getSqliteClerkTimeline = vi.fn()
      .mockReturnValueOnce(staleTimeline.promise)
      .mockResolvedValueOnce(timeline({ entries: [timelineEntry(11)] }));
    const view = await renderCustody(
      {
        getSqliteClerkProjection: vi.fn().mockResolvedValue(projection()),
        getSqliteClerkTimeline,
      },
      { timelineQuery: { orderRef: 'order:enter:12' } },
    );

    await waitFor(() => expect(getSqliteClerkTimeline).toHaveBeenCalledOnce());
    view.fixture.componentRef.setInput('target', resourceTarget('alpaca', 'clrk_other', {
      accountId: 'PA1', bindingGeneration: 5, routingEpoch: 2,
    }));
    view.fixture.componentRef.setInput('timelineQuery', { orderRef: 'order:enter:11' });
    view.fixture.detectChanges();
    await waitFor(() => expect(getSqliteClerkTimeline).toHaveBeenCalledTimes(2));
    staleTimeline.reject(new Error('stale lane unavailable'));

    expect((await screen.findAllByText('effect:enter:11')).length).toBeGreaterThan(0);
    expect(screen.queryByText('The custody timeline is temporarily unavailable.')).toBeNull();
  });

  it('explains when an exact evidence filter has no matching immutable transition', async () => {
    const query: SqliteTimelineQuery = { executionId: 'missing-execution' };
    await renderCustody(
      {
        getSqliteClerkProjection: vi.fn().mockResolvedValue(projection()),
        getSqliteClerkTimeline: vi.fn().mockResolvedValue(timeline({
          entries: [],
          total_entries: 0,
        })),
      },
      { timelineQuery: query },
    );

    expect(
      await screen.findByText('No immutable transitions match these exact filters.'),
    ).toBeTruthy();
    expect(screen.queryByLabelText('Selected immutable evidence')).toBeNull();
  });

  it('labels an unverified activation identity without presenting generation zero', async () => {
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue({
        ...projection([]),
        authority_generation: 0,
        db_identity_token: 'unverified-activation',
        authority_health: 'failed',
      }),
    });

    expect(await screen.findByText(/activation identity unverified/i)).toBeTruthy();
    expect(screen.queryByText(/generation 0/i)).toBeNull();
  });

  it('confirms and executes the exact evidence-bound mutation', async () => {
    const cancel = action({
      action_id: 'cancel_verified_working_orders',
      label: 'Cancel verified working orders',
      explanation: 'Cancel only order:entry:12.',
      freshness: 'fresh',
      evidence: [{
        reference: 'order:order:entry:12',
        label: 'Verified working order',
        observed_at_ms: NOW - 100,
        age_ms: 100,
        freshness: 'fresh',
      }],
      confirmation: {
        title: 'Cancel verified orders?',
        explanation: 'Only the listed order will be canceled.',
        confirm_label: 'Cancel verified orders',
      },
    });
    const receipt = {
      action_id: cancel.action_id,
      outcome: 'success',
      applied: true,
      receipt_id: 'order:entry:12',
      recorded_at_ms: NOW + 20,
      command: null,
      reconciliation: null,
      orders: [],
    } satisfies SqliteRecoveryResult;
    const executeSqliteRecoveryAction = vi.fn().mockResolvedValue(receipt);
    const { fixture } = await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([cancel])),
      executeSqliteRecoveryAction,
    });

    fireEvent.click(await screen.findByRole('button', { name: cancel.label }));
    const confirmation = fixture.debugElement.query(By.directive(TypedHaltConfirmComponent));
    expect(confirmation).toBeTruthy();
    confirmation.componentInstance.confirmed.emit();

    await waitFor(() => {
      expect(executeSqliteRecoveryAction).toHaveBeenCalledWith(
        expect.objectContaining({ clerkId: 'clrk_spec', accountId: 'PA1', capability: 'custody_command' }), cancel,
      );
    });
    expect(await screen.findByText('order:entry:12')).toBeTruthy();
  });

  /** #2188: a confirmation is one of the two surfaces where the account
   * NUMBER earns its place. These recovery confirmations carry backend-
   * composed copy that names no account (`recovery_policy.py`'s descriptors
   * are module-level constants with no context), so the number has to reach
   * the dialog as a threaded fact from the host that already knows it — the
   * same footing as the asset symbol beside it, never prose composed here. */
  it('names the account by number in an evidence-bound confirmation', async () => {
    const flatten = action({
      action_id: 'execute_safe_flatten',
      label: 'Execute safe flatten',
      explanation: 'Submit the prepared exact reduction as recovery EXIT custody.',
      freshness: 'fresh',
      confirmation: {
        title: 'Flatten attributed exposure?',
        explanation: 'The Clerk will submit reduction-only orders.',
        confirm_label: 'Flatten now',
      },
    });
    const { fixture } = await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([flatten])),
    });

    fireEvent.click(await screen.findByRole('button', { name: flatten.label }));
    fixture.detectChanges();

    const dialog = await screen.findByRole('dialog');
    expect(dialog.textContent).toContain('Flatten attributed exposure?');
    expect(dialog.textContent).toContain('PA1');
  });

  /** #2106: `target` arrives as an `input()` sourced from the live fleet
   * directory. If the lane rebinds between when the operator opened the
   * confirmation and when they confirm it, minting the command from
   * `target()` at either point would silently carry the new (wrong)
   * generation. The frozen `fence` input, not `target()`'s live generation,
   * must decide what is sent. */
  it('sends the generation shown when the action opened, not the one current at confirm', async () => {
    const cancel = action({
      action_id: 'cancel_verified_working_orders',
      label: 'Cancel verified working orders',
      explanation: 'Cancel only order:entry:12.',
      freshness: 'fresh',
      evidence: [{
        reference: 'order:order:entry:12',
        label: 'Verified working order',
        observed_at_ms: NOW - 100,
        age_ms: 100,
        freshness: 'fresh',
      }],
      confirmation: {
        title: 'Cancel verified orders?',
        explanation: 'Only the listed order will be canceled.',
        confirm_label: 'Cancel verified orders',
      },
    });
    const receipt = {
      action_id: cancel.action_id,
      outcome: 'success',
      applied: true,
      receipt_id: 'order:entry:12',
      recorded_at_ms: NOW + 20,
      command: null,
      reconciliation: null,
      orders: [],
    } satisfies SqliteRecoveryResult;
    const executeSqliteRecoveryAction = vi.fn().mockResolvedValue(receipt);
    const { fixture } = await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([cancel])),
      executeSqliteRecoveryAction,
    });

    await screen.findByRole('button', { name: cancel.label });
    // The lane rebinds (a directory refresh) after the panel rendered but
    // before the operator opens the confirmation. `target()` now reports the
    // new generation; the fence frozen at render time, `fence`, does not
    // move — the command minted when the confirmation opens must still carry
    // the generation the operator was shown.
    fixture.componentRef.setInput('target', resourceTarget('alpaca', 'clrk_spec', {
      accountId: 'PA1', bindingGeneration: 99, routingEpoch: 55,
    }));
    fixture.detectChanges();
    fireEvent.click(await screen.findByRole('button', { name: cancel.label }));
    const confirmation = fixture.debugElement.query(By.directive(TypedHaltConfirmComponent));
    confirmation.componentInstance.confirmed.emit();

    await waitFor(() => {
      expect(executeSqliteRecoveryAction).toHaveBeenCalledWith(
        expect.objectContaining({ bindingGeneration: 4, routingEpoch: 1 }), cancel,
      );
    });
  });

  /** #2106: a cold directory at render time freezes `fence` with a null
   * generation; `commandContextOf` sends no generation check at all for one.
   * Refuse rather than dispatch blind. */
  it('refuses a mutating action when the lane had no known binding at render', async () => {
    const cancel = action({
      action_id: 'cancel_verified_working_orders',
      label: 'Cancel verified working orders',
      explanation: 'Cancel only order:entry:12.',
      freshness: 'fresh',
      evidence: [],
      confirmation: null,
    });
    const executeSqliteRecoveryAction = vi.fn().mockResolvedValue({});
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([cancel])),
      executeSqliteRecoveryAction,
    }, { fence: { bindingGeneration: null, routingEpoch: null } });

    fireEvent.click(await screen.findByRole('button', { name: cancel.label }));

    expect(executeSqliteRecoveryAction).not.toHaveBeenCalled();
    expect(await screen.findByText(/no known binding when the action was opened/i)).toBeTruthy();
  });

  it('refreshes and renders the safe-flatten plan without executing a mutation', async () => {
    const prepare = action({
      action_id: 'prepare_safe_flatten',
      label: 'Prepare safe flatten',
      explanation: 'Prepare a fresh reduction plan without submitting an order.',
      freshness: 'fresh',
      mutation: false,
      primary: false,
    });
    const checkSqliteRecoveryAction = vi.fn().mockResolvedValue({
      ...prepare,
      reduction_plan: SAFE_FLATTEN_PLAN,
      next_step: 'Review the exact attributed quantity in the plan.',
    });
    const executeSqliteRecoveryAction = vi.fn();
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([prepare])),
      checkSqliteRecoveryAction,
      executeSqliteRecoveryAction,
    });

    fireEvent.click(await screen.findByRole('button', { name: prepare.label }));

    expect(await screen.findByRole('region', {
      name: 'Prepared safe-flatten reduction plan',
    })).toBeTruthy();
    expect(screen.getByText('Spy')).toBeTruthy();
    expect(screen.getByText('1.25')).toBeTruthy();
    expect(checkSqliteRecoveryAction).toHaveBeenCalledWith('clrk_spec', 'PA1', prepare);
    expect(executeSqliteRecoveryAction).not.toHaveBeenCalled();
  });

  it('never auto-retries a stale action and refreshes the projection', async () => {
    const getSqliteClerkProjection = vi.fn().mockResolvedValue(projection());
    const executeSqliteRecoveryAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({ status: 409 }),
    );
    await renderCustody({
      getSqliteClerkProjection,
      executeSqliteRecoveryAction,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Reconcile now' }));

    expect(await screen.findByText(/Clerk evidence changed/)).toBeTruthy();
    expect(executeSqliteRecoveryAction).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(getSqliteClerkProjection).toHaveBeenCalledTimes(2));
  });

  it('renders the backend reason, message, and remediation on a typed refusal', async () => {
    const executeSqliteRecoveryAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            reason: 'stale_action_token',
            message: 'The evidence changed after this action was presented.',
            next_step: 'Refresh and review the current Clerk action.',
          },
        },
      }),
    );
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection()),
      executeSqliteRecoveryAction,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Reconcile now' }));

    expect(await screen.findByText('Stale Action Token')).toBeTruthy();
    expect(screen.getByText('The evidence changed after this action was presented.')).toBeTruthy();
    expect(screen.getByText('Refresh and review the current Clerk action.')).toBeTruthy();
  });

  it('renders a recovery capability nested next step on an unavailable action', async () => {
    const executeSqliteRecoveryAction = vi.fn().mockRejectedValue(
      new HttpErrorResponse({
        status: 409,
        error: {
          detail: {
            reason: 'recovery_action_unavailable',
            message: 'The presented recovery action is no longer available.',
            capability: {
              next_step: 'Review the refreshed recovery capability before trying again.',
            },
          },
        },
      }),
    );
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection()),
      executeSqliteRecoveryAction,
    });

    fireEvent.click(await screen.findByRole('button', { name: 'Reconcile now' }));

    expect(
      await screen.findByText('Review the refreshed recovery capability before trying again.'),
    ).toBeTruthy();
  });

  it('renders policy-authored unavailability and no generic recovery action', async () => {
    const unavailable = action({
      action_id: 'cancel_verified_working_orders',
      label: 'Cancel verified working orders',
      available: false,
      unavailable_reason_code: 'NO_VERIFIED_WORKING_ORDERS',
      unavailable_reason: 'No exact working-order identity is proven.',
      primary: false,
    });
    await renderCustody({
      getSqliteClerkProjection: vi.fn().mockResolvedValue(projection([unavailable])),
    });

    const button = await screen.findByRole('button', { name: unavailable.label });
    expect(button.hasAttribute('disabled')).toBe(true);
    expect(screen.getByText('No exact working-order identity is proven.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: /clear hold/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /retry/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /flatten/i })).toBeNull();
  });
});
