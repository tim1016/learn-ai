import { HttpErrorResponse } from '@angular/common/http';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type {
  CohortActionResult,
  CohortFlattenCohort,
  CohortFlattenLeg,
  CohortFlattenView,
  CohortLegResult,
} from '../lib/broker-v2-panel.types';
import { CohortFlattenDrawerComponent } from './cohort-flatten-drawer.component';
import { provideFleetDirectory, testLane } from '../../../../fleet/fleet-directory-testing';
import { formatReceiptLabel } from '../../../../shared/pipes/receipt-label.pipe';
import { MAX_COHORT_FLATTEN_LEGS } from './cohort-flatten-confirmation';

function leg(overrides: Partial<CohortFlattenLeg> = {}): CohortFlattenLeg {
  return {
    strategy_instance_id: 'qqq-1',
    action_id: 'execute_safe_flatten',
    enabled: true,
    revision: 3,
    concurrency_token: 'tok-1',
    blocker_headline: null,
    exposure: { QQQ: 10 },
    ...overrides,
  };
}

function cohort(
  legs: CohortFlattenLeg[],
  overrides: Partial<CohortFlattenCohort> = {},
): CohortFlattenCohort {
  return {
    strategy_key: 'deployment_validation',
    strategy_label: 'Deployment Validation',
    symbol: 'QQQ',
    legs,
    enabled_count: legs.filter((item) => item.enabled).length,
    ...overrides,
  };
}

function view(cohorts: CohortFlattenCohort[]): CohortFlattenView {
  return { account_id: 'PA1', observed_at_ms: 1_700_000_000_000, cohorts };
}

const QQQ_COHORT = cohort([
  leg(),
  leg({ strategy_instance_id: 'qqq-2', revision: 5, concurrency_token: 'tok-2', exposure: { QQQ: -4 } }),
  leg({
    strategy_instance_id: 'qqq-3',
    enabled: false,
    revision: null,
    concurrency_token: null,
    blocker_headline: 'This bot is still running. Stop it first.',
    exposure: { QQQ: 2 },
  }),
]);

const SPY_COHORT = cohort(
  [
    leg({ strategy_instance_id: 'spy-1', concurrency_token: 'tok-s1', exposure: { SPY: 1 } }),
    leg({ strategy_instance_id: 'spy-2', concurrency_token: 'tok-s2', exposure: { SPY: 1 } }),
  ],
  { symbol: 'SPY' },
);

function applied(sid: string, receipt = `receipt-${sid}`): CohortLegResult {
  return {
    strategy_instance_id: sid,
    outcome: 'applied',
    result: {
      action_id: 'execute_safe_flatten',
      outcome: 'success',
      receipt_id: receipt,
      recorded_at_ms: 1_700_000_000_500,
      applied: true,
      revision: 4,
      concurrency_token: 'next',
      message: `Flatten submitted for ${sid}.`,
    },
    error: null,
  };
}

function refused(
  sid: string,
  overrides: Partial<NonNullable<CohortLegResult['error']>> = {},
  outcome: CohortLegResult['outcome'] = 'refused',
): CohortLegResult {
  return {
    strategy_instance_id: sid,
    outcome,
    result: null,
    error: {
      action_id: 'execute_safe_flatten',
      outcome: outcome === 'refused' ? 'conflict' : outcome === 'unknown' ? 'unknown' : 'failure',
      receipt_id: null,
      recorded_at_ms: 1,
      message: 'This action changed since it was presented.',
      why: 'The bot resumed after the roster was read.',
      reason_code: 'STALE_REVISION',
      ...overrides,
    },
  };
}

/** A refusal a same-key retry can change: the lease the batch hit was revived. */
function revived(sid: string): CohortLegResult {
  return refused(sid, {
    message: 'The execution lease was revived; this request applied nothing.',
    why: 'Click the action again.',
    reason_code: 'EXECUTION_LEASE_REVIVED',
  });
}

function result(legs: CohortLegResult[]): CohortActionResult {
  const count = (...kinds: string[]) => legs.filter((item) => kinds.includes(item.outcome)).length;
  return {
    account_id: 'PA1',
    receipt_id: 'batch',
    recorded_at_ms: 1_700_000_001_000,
    legs,
    applied_count: count('applied'),
    replayed_count: count('replayed'),
    refused_count: count('refused'),
    failed_count: count('failed', 'unknown'),
  };
}

function fakeService(cohorts: CohortFlattenCohort[], batch = result([applied('qqq-1'), applied('qqq-2')])) {
  return {
    getCohortFlattenView: vi.fn().mockResolvedValue(view(cohorts)),
    runCohortFlatten: vi.fn().mockResolvedValue(batch),
  };
}

function open(
  service: ReturnType<typeof fakeService>,
  overrides: { directory?: ReturnType<typeof provideFleetDirectory> } = {},
) {
  const directory =
    overrides.directory ??
    provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
  return render(CohortFlattenDrawerComponent, {
    inputs: { clerkId: 'clrk_spec', visible: true, broker: 'alpaca', accountId: 'PA1' },
    providers: [
      { provide: directory.provide, useValue: directory.useValue },
      { provide: BrokerV2PanelService, useValue: service },
    ],
  });
}

function checkbox(sid: string): HTMLInputElement {
  return screen.getByRole('checkbox', { name: sid }) as HTMLInputElement;
}

async function confirmWave(user: ReturnType<typeof userEvent.setup>, count: number): Promise<void> {
  await user.click(screen.getByRole('button', { name: `Review flatten of ${count}` }));
  const dialog = await screen.findByRole('dialog');
  await user.type(within(dialog).getByRole('textbox'), 'FLATTEN');
  await user.click(within(dialog).getByRole('button', { name: `Flatten ${count}` }));
}

interface Deferred<T> {
  readonly promise: Promise<T>;
  resolve(value: T): void;
  reject(error: unknown): void;
}

function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  let reject!: (error: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Each presentation read gets its own deferred, settled by the spec. */
function queuedReads(service: ReturnType<typeof fakeService>): Deferred<CohortFlattenView>[] {
  const reads: Deferred<CohortFlattenView>[] = [];
  service.getCohortFlattenView = vi.fn(() => {
    const read = deferred<CohortFlattenView>();
    reads.push(read);
    return read.promise;
  });
  return reads;
}

function reviewButton(count: number): HTMLButtonElement {
  return screen.getByRole('button', { name: `Review flatten of ${count}` }) as HTMLButtonElement;
}

afterEach(() => vi.restoreAllMocks());

describe('CohortFlattenDrawerComponent', () => {
  it('renders each cohort with its legs, attributed exposure, and the blocker of a disabled leg', async () => {
    await open(fakeService([QQQ_COHORT]));

    expect(await screen.findByText('Deployment Validation')).toBeTruthy();
    expect(screen.getByText('+10 QQQ')).toBeTruthy();
    expect(screen.getByText('-4 QQQ')).toBeTruthy();
    expect(screen.getByText('This bot is still running. Stop it first.')).toBeTruthy();
    expect(checkbox('qqq-3').disabled).toBe(true);
  });

  it('says the read failed, never that there are no cohorts, when the GET fails', async () => {
    const service = fakeService([]);
    service.getCohortFlattenView = vi.fn().mockRejectedValue(new Error('503'));

    await open(service);

    expect((await screen.findByRole('alert')).textContent).toContain('Could not read');
    expect(screen.queryByText(/No cohort on this account/)).toBeNull();
  });

  it('says so plainly when the account has no multi-bot cohort', async () => {
    await open(fakeService([]));

    expect(await screen.findByText(/No cohort on this account/)).toBeTruthy();
  });

  it('defaults the selection to the armed legs of one cohort', async () => {
    await open(fakeService([QQQ_COHORT, SPY_COHORT]));

    await screen.findAllByRole('checkbox');
    expect(checkbox('qqq-1').checked).toBe(true);
    expect(checkbox('qqq-2').checked).toBe(true);
    expect(checkbox('qqq-3').checked).toBe(false);
    // Another cohort's legs are never swept into this wave.
    expect(checkbox('spy-1').checked).toBe(false);
    expect(checkbox('spy-1').disabled).toBe(true);
    expect(screen.getByRole('button', { name: 'Review flatten of 2' })).toBeTruthy();
  });

  it('switches the wave to another cohort only through that cohort’s own button', async () => {
    await open(fakeService([QQQ_COHORT, SPY_COHORT]));
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await user.click(screen.getByRole('button', { name: /Select this cohort: Deployment Validation SPY/ }));

    expect(checkbox('spy-1').checked).toBe(true);
    expect(checkbox('spy-2').checked).toBe(true);
    expect(checkbox('qqq-1').checked).toBe(false);
  });

  it('arms a cohort’s button only when the backend armed one of its legs', async () => {
    const blocked = cohort(
      [
        leg({ strategy_instance_id: 'iwm-1', enabled: false, revision: null, concurrency_token: null, blocker_headline: 'Running.' }),
        leg({ strategy_instance_id: 'iwm-2', enabled: false, revision: null, concurrency_token: null, blocker_headline: 'Running.' }),
      ],
      { symbol: 'IWM' },
    );
    await open(fakeService([QQQ_COHORT, blocked]));
    await screen.findAllByRole('checkbox');

    const button = screen.getByRole('button', {
      name: /Select this cohort: Deployment Validation IWM/,
    }) as HTMLButtonElement;
    expect(button.disabled).toBe(true);
  });

  it('shows the blast radius and requires the typed token before anything is sent', async () => {
    const service = fakeService([QQQ_COHORT]);
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await user.click(screen.getByRole('button', { name: 'Review flatten of 2' }));
    const dialog = await screen.findByRole('dialog');

    expect(dialog.textContent).toContain('2 bots');
    expect(dialog.textContent).toContain('account PA1');
    expect(dialog.textContent).toContain('qqq-1 +10 QQQ');
    expect(dialog.textContent).toContain('qqq-2 -4 QQQ');
    expect(dialog.textContent).not.toContain('qqq-3');
    const commit = within(dialog).getByRole('button', { name: 'Flatten 2' }) as HTMLButtonElement;
    expect(commit.disabled).toBe(true);
    expect(service.runCohortFlatten).not.toHaveBeenCalled();
  });

  it('returns focus to the review control when the confirmation is cancelled', async () => {
    const service = fakeService([QQQ_COHORT]);
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await user.click(screen.getByRole('button', { name: 'Review flatten of 2' }));
    const dialog = await screen.findByRole('dialog');
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }));

    expect(screen.queryByRole('dialog')).toBeNull();
    await vi.waitFor(() =>
      expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Review flatten of 2' })),
    );
    expect(service.runCohortFlatten).not.toHaveBeenCalled();
  });

  it('sends exactly the confirmed legs, each with the identity it was presented with', async () => {
    const service = fakeService([QQQ_COHORT]);
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await user.click(checkbox('qqq-1'));
    await confirmWave(user, 1);

    expect(service.runCohortFlatten).toHaveBeenCalledTimes(1);
    const [target, request] = service.runCohortFlatten.mock.calls[0];
    expect(target).toMatchObject({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA1' });
    expect(target.idempotencyKey).toBe(request.idempotency_key);
    expect(request.legs).toEqual([
      {
        strategy_instance_id: 'qqq-2',
        action_id: 'execute_safe_flatten',
        revision: 5,
        concurrency_token: 'tok-2',
      },
    ]);
  });

  it('renders every leg outcome in request order: receipts, and typed refusals through the label pipe', async () => {
    const service = fakeService(
      [QQQ_COHORT],
      result([applied('qqq-1', 'rcpt/0001'), refused('qqq-2')]),
    );
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 2);

    const outcome = await screen.findByRole('region', { name: 'Flatten outcome' });
    const items = within(outcome).getAllByRole('listitem');
    expect(items.map((item) => item.querySelector('code')?.textContent)).toEqual(['qqq-1', 'qqq-2']);
    expect(within(items[0]).getByText('rcpt/0001')).toBeTruthy();
    expect(within(items[0]).getByText('Flatten submitted for qqq-1.')).toBeTruthy();
    expect(within(items[1]).getByText(formatReceiptLabel('STALE_REVISION'))).toBeTruthy();
    expect(within(items[1]).queryByText('STALE_REVISION')).toBeNull();
    expect(within(items[1]).getByText('This action changed since it was presented.')).toBeTruthy();
    expect(within(items[1]).getByText('The bot resumed after the roster was read.')).toBeTruthy();
  });

  it('renders an account-scoped early exit as the account blocker, keeping attempted outcomes', async () => {
    const three = cohort([
      leg(),
      leg({ strategy_instance_id: 'qqq-2', concurrency_token: 'tok-2' }),
      leg({ strategy_instance_id: 'qqq-4', concurrency_token: 'tok-4' }),
    ]);
    const service = fakeService(
      [three],
      result([
        applied('qqq-1'),
        refused(
          'qqq-2',
          {
            message: 'This account’s execution lease is lost.',
            why: 'Another process owns this account.',
            reason_code: 'EXECUTION_LEASE_LOST',
          },
          'failed',
        ),
      ]),
    );
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 3);

    const blocker = await screen.findByRole('alert', { name: 'Batch stopped: account-scoped' });
    expect(blocker.textContent).toContain('after 2 of 3 bots');
    expect(within(blocker).getByText(formatReceiptLabel('EXECUTION_LEASE_LOST'))).toBeTruthy();
    expect(within(blocker).getByText('Another process owns this account.')).toBeTruthy();
    const outcome = screen.getByRole('region', { name: 'Flatten outcome' });
    expect(within(outcome).getByText(/Not attempted/).textContent).toContain('qqq-4');
    expect(within(outcome).getByText('receipt-qqq-1')).toBeTruthy();
  });

  it('retries the same wave under the same durable key and the same legs', async () => {
    const service = fakeService([QQQ_COHORT], result([applied('qqq-1'), revived('qqq-2')]));
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 2);
    await user.click(await screen.findByRole('button', { name: 'Retry this batch' }));

    expect(service.runCohortFlatten).toHaveBeenCalledTimes(2);
    const [firstTarget, firstRequest] = service.runCohortFlatten.mock.calls[0];
    const [retryTarget, retryRequest] = service.runCohortFlatten.mock.calls[1];
    expect(retryTarget).toBe(firstTarget);
    expect(retryRequest).toBe(firstRequest);
    expect(retryRequest.idempotency_key).toBe(firstRequest.idempotency_key);
  });

  it('offers no retry once every leg applied or replayed', async () => {
    const service = fakeService([QQQ_COHORT]);
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 2);

    await screen.findByRole('region', { name: 'Flatten outcome' });
    expect(screen.queryByRole('button', { name: 'Retry this batch' })).toBeNull();
  });

  it('reports an unknown outcome when the POST never lands, and retries under the same key', async () => {
    const service = fakeService([QQQ_COHORT]);
    service.runCohortFlatten = vi
      .fn()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(result([applied('qqq-1'), applied('qqq-2')]));
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 2);

    expect((await screen.findByRole('alert')).textContent).toContain('did not reach a result');
    await user.click(screen.getByRole('button', { name: 'Retry this batch' }));
    const [, first] = service.runCohortFlatten.mock.calls[0];
    const [, retry] = service.runCohortFlatten.mock.calls[1];
    expect(retry.idempotency_key).toBe(first.idempotency_key);
  });

  it('mints a new key for a genuinely new wave from the re-read presentation', async () => {
    const service = fakeService([QQQ_COHORT], result([applied('qqq-1'), refused('qqq-2')]));
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');

    await confirmWave(user, 2);
    await screen.findByRole('region', { name: 'Flatten outcome' });
    // The drawer re-read the presentation after the batch.
    await vi.waitFor(() => expect(service.getCohortFlattenView).toHaveBeenCalledTimes(2));
    await confirmWave(user, 2);

    expect(service.runCohortFlatten).toHaveBeenCalledTimes(2);
    const [, first] = service.runCohortFlatten.mock.calls[0];
    const [, second] = service.runCohortFlatten.mock.calls[1];
    expect(second.idempotency_key).not.toBe(first.idempotency_key);
  });

  it('dispatches nothing when the drawer opened against a cold directory', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: null })],
    });
    const service = fakeService([QQQ_COHORT]);
    await open(service, { directory });
    await screen.findAllByRole('checkbox');

    expect(screen.getByText(/no known binding when the action was opened/i)).toBeTruthy();
    expect(
      (screen.getByRole('button', { name: 'Review flatten of 2' }) as HTMLButtonElement).disabled,
    ).toBe(true);
    expect(service.runCohortFlatten).not.toHaveBeenCalled();
  });

  describe('wave boundaries (#1909 review round)', () => {
    it('keeps Review disabled until a presentation read STARTED after the POST resolved', async () => {
      const service = fakeService([QQQ_COHORT]);
      const reads = queuedReads(service);
      const posts = [deferred<CohortActionResult>(), deferred<CohortActionResult>()];
      service.runCohortFlatten = vi
        .fn()
        .mockReturnValueOnce(posts[0].promise)
        .mockReturnValueOnce(posts[1].promise);
      const { fixture } = await open(service);
      const user = userEvent.setup();
      reads[0].resolve(view([QQQ_COHORT]));
      await screen.findAllByRole('checkbox');

      await confirmWave(user, 2);
      posts[0].resolve(result([applied('qqq-1'), revived('qqq-2')]));
      // The first POST resolved: read 2 starts and stays in flight.
      await vi.waitFor(() => expect(reads).toHaveLength(2));
      await user.click(await screen.findByRole('button', { name: 'Retry this batch' }));
      // The retry resolves while read 2 — started BEFORE it resolved — is
      // still loading, so read 2 may carry pre-flatten facts.
      posts[1].resolve(result([applied('qqq-1'), applied('qqq-2')]));
      await vi.waitFor(() => expect(screen.queryByText(/Flattening \d+ bots?…/)).toBeNull());
      reads[1].resolve(view([QQQ_COHORT]));
      await vi.waitFor(() => expect(reads).toHaveLength(3));
      fixture.detectChanges();

      expect(reviewButton(2).disabled).toBe(true);

      reads[2].resolve(view([QQQ_COHORT]));
      await vi.waitFor(() => expect(reviewButton(2).disabled).toBe(false));
    });

    it('does not re-read, and still dispatches, when the directory refreshes an identical lane while confirm is open', async () => {
      const directory = provideFleetDirectory({
        observed_at_ms: 1_757_000_000_000,
        clerks: [testLane({ clerk_id: 'clrk_spec' })],
      });
      const service = fakeService([QQQ_COHORT]);
      const { fixture } = await open(service, { directory });
      const user = userEvent.setup();
      await screen.findAllByRole('checkbox');

      await user.click(reviewButton(2));
      const dialog = await screen.findByRole('dialog');
      await user.type(within(dialog).getByRole('textbox'), 'FLATTEN');
      directory.rebind({
        observed_at_ms: 1_757_000_000_001,
        clerks: [testLane({ clerk_id: 'clrk_spec', observed_at_ms: 1_757_000_000_001 })],
      });
      await directory.useValue.refresh?.();
      await fixture.whenStable();
      fixture.detectChanges();

      // On demand, not polled (ADR 0051 D3): an unchanged lane is no reason to re-read.
      expect(service.getCohortFlattenView).toHaveBeenCalledTimes(1);
      await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Flatten 2' }));
      expect(service.runCohortFlatten).toHaveBeenCalledTimes(1);
    });

    it('closes the confirmation and sends nothing when the lane rebinds while it is open', async () => {
      const directory = provideFleetDirectory({
        observed_at_ms: 1_757_000_000_000,
        clerks: [testLane({ clerk_id: 'clrk_spec' })],
      });
      const service = fakeService([QQQ_COHORT]);
      const { fixture } = await open(service, { directory });
      const user = userEvent.setup();
      await screen.findAllByRole('checkbox');

      await user.click(reviewButton(2));
      const dialog = await screen.findByRole('dialog');
      await user.type(within(dialog).getByRole('textbox'), 'FLATTEN');
      const commit = within(dialog).getByRole('button', { name: 'Flatten 2' });
      directory.rebind({
        observed_at_ms: 1_757_000_000_001,
        clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 4 })],
      });
      await directory.useValue.refresh?.();
      await fixture.whenStable();
      fixture.detectChanges();
      // A click that raced the conflict must not dispatch either.
      commit.click();
      await fixture.whenStable();

      expect(screen.queryByRole('dialog')).toBeNull();
      expect(await screen.findByText(/rebound while the action was open/i)).toBeTruthy();
      expect(service.runCohortFlatten).not.toHaveBeenCalled();
    });
  });

  describe('failure honesty (#1909 review round)', () => {
    it('keeps the known per-leg receipts when a retry never lands', async () => {
      const service = fakeService([QQQ_COHORT]);
      service.runCohortFlatten = vi
        .fn()
        .mockResolvedValueOnce(result([applied('qqq-1', 'rcpt/keep'), revived('qqq-2')]))
        .mockRejectedValueOnce(new Error('network down'));
      await open(service);
      const user = userEvent.setup();
      await screen.findAllByRole('checkbox');

      await confirmWave(user, 2);
      await user.click(await screen.findByRole('button', { name: 'Retry this batch' }));

      expect(await screen.findByText(/did not reach a result/)).toBeTruthy();
      expect(screen.getByText('rcpt/keep')).toBeTruthy();
    });

    it('moves focus to the unknown-outcome alert when the POST never lands', async () => {
      const service = fakeService([QQQ_COHORT]);
      service.runCohortFlatten = vi.fn().mockRejectedValue(new Error('network down'));
      await open(service);
      const user = userEvent.setup();
      await screen.findAllByRole('checkbox');

      await confirmWave(user, 2);

      const alert = await screen.findByText(/did not reach a result/);
      await vi.waitFor(() => expect(alert.closest('[tabindex="-1"]')).toBe(document.activeElement));
    });

    it('moves focus to the outcome once the wave answers', async () => {
      const service = fakeService([QQQ_COHORT]);
      await open(service);
      const user = userEvent.setup();
      await screen.findAllByRole('checkbox');

      await confirmWave(user, 2);

      const outcome = await screen.findByRole('region', { name: 'Flatten outcome' });
      await vi.waitFor(() => expect(document.activeElement).toBe(outcome));
    });

    it('renders a typed batch refusal as a refusal, refreshes the directory, and offers no retry', async () => {
      const directory = provideFleetDirectory({
        observed_at_ms: 1_757_000_000_000,
        clerks: [testLane({ clerk_id: 'clrk_spec' })],
      });
      const refresh = vi.spyOn(directory.useValue as never, 'refresh');
      const service = fakeService([QQQ_COHORT]);
      service.runCohortFlatten = vi.fn().mockRejectedValue(
        new HttpErrorResponse({
          status: 409,
          error: { reason: 'clerk_binding_generation_conflict', message: 'Expected 3 is not 4.' },
        }),
      );
      await open(service, { directory });
      const user = userEvent.setup();
      await screen.findAllByRole('checkbox');

      await confirmWave(user, 2);

      expect(await screen.findByText(/Expected 3 is not 4\./)).toBeTruthy();
      expect(screen.queryByText(/Nothing is confirmed either way/)).toBeNull();
      expect(screen.queryByRole('button', { name: 'Retry this batch' })).toBeNull();
      await vi.waitFor(() => expect(refresh).toHaveBeenCalledTimes(1));
    });

    it('offers no same-key retry for stale-presentation refusals and says to start a new wave', async () => {
      // StaleRevisionError / ActionNotAvailableError are raised without a
      // reason_code; re-sending the same tokens can only be refused again.
      const service = fakeService(
        [QQQ_COHORT],
        result([applied('qqq-1'), refused('qqq-2', { reason_code: null })]),
      );
      await open(service);
      const user = userEvent.setup();
      await screen.findAllByRole('checkbox');

      await confirmWave(user, 2);

      await screen.findByRole('region', { name: 'Flatten outcome' });
      expect(screen.queryByRole('button', { name: 'Retry this batch' })).toBeNull();
      expect(screen.getByText(/start a new wave/i)).toBeTruthy();
    });

    it('names legs the response never answered even without an account blocker', async () => {
      // A contract violation (fewer legs than sent, no terminal refusal) must
      // be visible, not silently dropped.
      const service = fakeService([QQQ_COHORT], result([applied('qqq-1')]));
      await open(service);
      const user = userEvent.setup();
      await screen.findAllByRole('checkbox');

      await confirmWave(user, 2);

      const outcome = await screen.findByRole('region', { name: 'Flatten outcome' });
      expect(within(outcome).getByText(/Not attempted/).textContent).toContain('qqq-2');
      expect(screen.queryByRole('alert', { name: 'Batch stopped: account-scoped' })).toBeNull();
    });
  });

  it('caps a wave at the backend’s leg limit', async () => {
    const legs = Array.from({ length: MAX_COHORT_FLATTEN_LEGS + 1 }, (_, index) =>
      leg({ strategy_instance_id: `qqq-${String(index).padStart(3, '0')}`, concurrency_token: `t${index}` }),
    );
    await open(fakeService([cohort(legs)]));
    await screen.findAllByRole('checkbox');

    expect(reviewButton(MAX_COHORT_FLATTEN_LEGS)).toBeTruthy();
    const overflow = checkbox(`qqq-${String(MAX_COHORT_FLATTEN_LEGS).padStart(3, '0')}`);
    expect(overflow.checked).toBe(false);
    expect(overflow.disabled).toBe(true);
    expect(screen.getByText(new RegExp(`at most ${MAX_COHORT_FLATTEN_LEGS} bots`))).toBeTruthy();
  });

  it('has no detectable accessibility violations with cohorts and an outcome on screen', async () => {
    const service = fakeService([QQQ_COHORT], result([applied('qqq-1'), refused('qqq-2')]));
    await open(service);
    const user = userEvent.setup();
    await screen.findAllByRole('checkbox');
    await confirmWave(user, 2);
    await screen.findByRole('region', { name: 'Flatten outcome' });

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });
});
