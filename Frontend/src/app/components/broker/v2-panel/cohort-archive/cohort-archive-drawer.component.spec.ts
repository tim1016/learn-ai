import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { BrokerV2PanelService } from '../lib/broker-v2-panel.service';
import type {
  CohortActionResult,
  CohortArchiveLeg,
  CohortArchiveView,
} from '../lib/broker-v2-panel.types';
import { CohortArchiveDrawerComponent } from './cohort-archive-drawer.component';
import { provideFleetDirectory, testLane } from '../../../../fleet/fleet-directory-testing';

function leg(overrides: Partial<CohortArchiveLeg> = {}): CohortArchiveLeg {
  return {
    strategy_instance_id: 'spy-done-1',
    enabled: true,
    revision: 1,
    concurrency_token: 'token-1',
    blocker_headline: null,
    ...overrides,
  };
}

function view(legs: CohortArchiveLeg[]): CohortArchiveView {
  return {
    account_id: 'PA1',
    observed_at_ms: 1_700_000_000_000,
    cohorts: legs.length
      ? [
          {
            strategy_key: 'deployment_validation',
            strategy_label: 'Deployment Validation',
            symbol: 'SPY',
            legs,
            enabled_count: legs.filter((item) => item.enabled).length,
          },
        ]
      : [],
  };
}

function result(overrides: Partial<CohortActionResult> = {}): CohortActionResult {
  return {
    account_id: 'PA1',
    receipt_id: 'sweep-1',
    recorded_at_ms: 1_700_000_000_000,
    legs: [],
    applied_count: 1,
    replayed_count: 0,
    refused_count: 0,
    failed_count: 0,
    ...overrides,
  };
}

function fakeService(legs: CohortArchiveLeg[], batch = result()) {
  return {
    getCohortArchiveView: vi.fn().mockResolvedValue(view(legs)),
    runCohortArchive: vi.fn().mockResolvedValue(batch),
  };
}

function open(
  service: ReturnType<typeof fakeService>,
  overrides: { directory?: ReturnType<typeof provideFleetDirectory> } = {},
) {
  // The double's default lane must resolve for this file's routed clerkId
  // ('clrk_spec', not the shared fixture's TEST_CLERK_ID) so the fence
  // checks exercise a real lane rather than a permanently-missing one.
  const directory =
    overrides.directory ??
    provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
  return render(CohortArchiveDrawerComponent, {
    inputs: { clerkId: 'clrk_spec', visible: true, broker: 'alpaca', accountId: 'PA1' },
    providers: [
      { provide: directory.provide, useValue: directory.useValue },
      { provide: BrokerV2PanelService, useValue: service },
    ],
  });
}

afterEach(() => vi.restoreAllMocks());

describe('CohortArchiveDrawerComponent', () => {
  it('shows a bot it cannot archive, with the reason, rather than hiding it', async () => {
    // A surface whose job is "show me what I can clear" must not quietly
    // under-report the roster: a hidden bot reads as an absent one.
    const service = fakeService([
      leg(),
      leg({
        strategy_instance_id: 'spy-held-1',
        enabled: false,
        revision: null,
        concurrency_token: null,
        blocker_headline: 'This bot still holds custody.',
      }),
    ]);

    await open(service);

    expect(await screen.findByText('spy-held-1')).toBeTruthy();
    expect(screen.getByText('This bot still holds custody.')).toBeTruthy();
    const blocked = screen.getAllByRole('checkbox')[1] as HTMLInputElement;
    expect(blocked.disabled).toBe(true);
  });

  it('refuses to submit until the operator types the confirmation token', async () => {
    const service = fakeService([leg()]);
    await open(service);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[0]);
    const submit = screen.getByRole('button', { name: /Archive 1/ });
    expect((submit as HTMLButtonElement).disabled).toBe(true);

    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'ARCHIVE');

    expect((screen.getByRole('button', { name: /Archive 1/ }) as HTMLButtonElement).disabled).toBe(
      false,
    );
  });

  it('sends exactly the checked legs, each with the identity it was presented with', async () => {
    // ADR 0051 Decision 2: membership is explicit. The server must execute
    // what the operator actually saw, not a set it re-derived.
    const service = fakeService([
      leg(),
      leg({
        strategy_instance_id: 'spy-done-2',
        revision: 4,
        concurrency_token: 'token-2',
      }),
    ]);
    await open(service);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[1]);
    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'archive');
    await user.click(screen.getByRole('button', { name: /Archive 1/ }));

    expect(service.runCohortArchive).toHaveBeenCalledTimes(1);
    const [target, request] = service.runCohortArchive.mock.calls[0];
    expect(target).toMatchObject({ broker: 'alpaca', clerkId: 'clrk_spec', accountId: 'PA1' });
    expect(request.legs).toEqual([
      { strategy_instance_id: 'spy-done-2', revision: 4, concurrency_token: 'token-2' },
    ]);
  });

  it('reports every leg outcome, naming the ones that did not apply', async () => {
    const service = fakeService(
      [leg()],
      result({
        applied_count: 1,
        refused_count: 1,
        legs: [
          {
            strategy_instance_id: 'spy-done-2',
            outcome: 'refused',
            result: null,
            error: {
              action_id: 'archive',
              outcome: 'conflict',
              receipt_id: null,
              recorded_at_ms: 1,
              message: 'This action changed since it was presented.',
              why: null,
              reason_code: null,
            },
          },
        ],
      }),
    );
    await open(service);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[0]);
    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'ARCHIVE');
    await user.click(screen.getByRole('button', { name: /Archive 1/ }));

    expect((await screen.findByRole('alert')).textContent).toContain('refused 1');
    expect(screen.getByText(/This action changed since it was presented\./)).toBeTruthy();
  });

  it('keeps blocked bots and their reasons visible when none are armed', async () => {
    // An account whose stopped bots all hold exposure has candidates, each
    // with a backend-authored reason. Collapsing that into a summary would
    // hide exactly the identities and remediation the view includes them for.
    const service = fakeService([
      leg({
        strategy_instance_id: 'spy-held-1',
        enabled: false,
        revision: null,
        concurrency_token: null,
        blocker_headline: 'This bot still holds custody.',
      }),
    ]);

    await open(service);

    expect(await screen.findByText('spy-held-1')).toBeTruthy();
    expect(screen.getByText('This bot still holds custody.')).toBeTruthy();
    expect(screen.queryByText(/No bot on this account can be archived/)).toBeNull();
  });

  it('says so plainly when the account has no candidates at all', async () => {
    const service = fakeService([]);

    await open(service);

    expect(await screen.findByText(/No bot on this account can be archived/)).toBeTruthy();
  });

  it('reports an unknown outcome and keeps the selection when the POST never lands', async () => {
    // An irreversible command must not leave the button quietly re-enabling:
    // the operator cannot tell whether the request reached the server.
    const service = fakeService([leg()]);
    service.runCohortArchive = vi.fn().mockRejectedValue(new Error('network down'));
    await open(service);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[0]);
    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'ARCHIVE');
    await user.click(screen.getByRole('button', { name: /Archive 1/ }));

    expect((await screen.findByRole('alert')).textContent).toContain(
      'did not reach a result',
    );
    // Selection preserved, so a retry is deliberate rather than re-selected.
    expect(screen.getByRole('button', { name: /Archive 1/ })).toBeTruthy();
  });

  it('reuses the drawer-presentation target and durable key for a deliberate retry', async () => {
    const service = fakeService([leg()]);
    service.runCohortArchive = vi.fn()
      .mockRejectedValueOnce(new Error('network down'))
      .mockResolvedValueOnce(result());
    await open(service);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[0]);
    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'ARCHIVE');
    await user.click(screen.getByRole('button', { name: /Archive 1/ }));
    await screen.findByRole('alert');
    await user.click(screen.getByRole('button', { name: /Archive 1/ }));

    expect(service.runCohortArchive).toHaveBeenCalledTimes(2);
    const [firstTarget, firstRequest] = service.runCohortArchive.mock.calls[0];
    const [retryTarget, retryRequest] = service.runCohortArchive.mock.calls[1];
    expect(retryTarget).toBe(firstTarget);
    expect(retryRequest.idempotency_key).toBe(firstRequest.idempotency_key);
  });

  it('drops a selection that no longer names a present, armed leg', async () => {
    // Derived rather than reset: switching accounts, or a reload that disarms
    // a leg, must not leave a stale id able to arm the confirm button.
    const service = fakeService([leg()]);
    await open(service);
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[0]);
    expect(screen.getByRole('button', { name: /Archive 1/ })).toBeTruthy();

    service.getCohortArchiveView.mockResolvedValue(
      view([leg({ enabled: false, revision: null, concurrency_token: null })]),
    );
    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'ARCHIVE');
    await user.click(screen.getByRole('button', { name: /Archive 1/ }));

    expect(await screen.findByRole('button', { name: /Archive 0/ })).toBeTruthy();
  });

  it('states the conflict when the lane rebinds under an open archive confirmation', async () => {
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const service = fakeService([leg()]);
    const { fixture } = await open(service, { directory });
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[0]);
    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'ARCHIVE');
    // Proves the button would otherwise be enabled: without this, a
    // still-disabled button after rebind would pass whether or not the
    // drift check does anything.
    expect((screen.getByRole('button', { name: /Archive 1/ }) as HTMLButtonElement).disabled)
      .toBe(false);

    directory.rebind({
      observed_at_ms: 1_757_000_000_001,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: 4 })],
    });
    // `rebind()` only stages the replacement; `refresh()` promotes it to
    // what `lanesOf()`/`lane()` report, like the real service's next load.
    await directory.useValue.refresh?.();
    // A signal-backed rebind can transiently unmount and remount other
    // reactive consumers; stabilize and re-query rather than reuse a stale
    // element handle.
    await fixture.whenStable();
    fixture.detectChanges();

    expect(await screen.findByText(/rebound while the action was open/i)).toBeTruthy();
    expect((screen.getByRole('button', { name: /Archive 1/ }) as HTMLButtonElement).disabled)
      .toBe(true);
    // The typed confirmation is not silently discarded.
    expect((screen.getByLabelText(/Type ARCHIVE to confirm/) as HTMLInputElement).value)
      .toBe('ARCHIVE');

    await user.click(screen.getByRole('button', { name: /Archive 1/ }));
    expect(service.runCohortArchive).not.toHaveBeenCalled();
  });

  it('does not flag a conflict when the directory refreshes with the identical lane', async () => {
    // `FleetDirectoryService.lanesOf()` `.filter()`s a fresh array on every
    // response tick, and the parent `target()` computed rebuilds a fresh
    // `resourceTarget(...)` object literal from it — so a directory refresh
    // that changes nothing about the lane (an unrelated poll, or the #2068
    // mitigating `refresh()` this branch itself arms) must not read as a
    // rebind just because the object identity changed.
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec' })],
    });
    const service = fakeService([leg()]);
    const { fixture } = await open(service, { directory });
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[0]);
    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'ARCHIVE');
    expect((screen.getByRole('button', { name: /Archive 1/ }) as HTMLButtonElement).disabled)
      .toBe(false);

    // Same generation, same epoch — only the poll's observed_at_ms moved.
    directory.rebind({
      observed_at_ms: 1_757_000_000_001,
      clerks: [testLane({ clerk_id: 'clrk_spec', observed_at_ms: 1_757_000_000_001 })],
    });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(screen.queryByText(/rebound while the action was open/i)).toBeNull();
    expect((screen.getByRole('button', { name: /Archive 1/ }) as HTMLButtonElement).disabled)
      .toBe(false);

    await user.click(screen.getByRole('button', { name: /Archive 1/ }));
    expect(service.runCohortArchive).toHaveBeenCalledTimes(1);
  });

  it('refuses to submit when the drawer opened against a cold directory, and dispatches nothing', async () => {
    // Cold directory: the lane is present but its binding is unconfirmed, so
    // `freezeLaneFence` yields a null generation (#2068, decision 15). A
    // present-but-null lane, not an absent one: an absent lane would also
    // read as "drifted" by laneFenceDrifted, which would mask a deleted
    // enforceability branch behind the drift branch instead of proving it.
    const directory = provideFleetDirectory({
      observed_at_ms: 1_757_000_000_000,
      clerks: [testLane({ clerk_id: 'clrk_spec', effective_binding_generation: null })],
    });
    const service = fakeService([leg()]);
    const { fixture } = await open(service, { directory });
    const user = userEvent.setup();

    await user.click((await screen.findAllByRole('checkbox'))[0]);
    await user.type(screen.getByLabelText(/Type ARCHIVE to confirm/), 'ARCHIVE');
    await fixture.whenStable();
    fixture.detectChanges();

    // The dispatch assertion comes first and does not depend on the button's
    // disabled attribute: userEvent no-ops a click on a genuinely disabled
    // button, so this proves the command itself never reaches the service,
    // not merely that some other assertion (a disabled attribute, or the
    // banner copy) happens to read a certain way.
    await user.click(screen.getByRole('button', { name: /Archive 1/ }));
    expect(service.runCohortArchive).not.toHaveBeenCalled();
    expect(screen.getByText(/no known binding when the action was opened/i)).toBeTruthy();
  });
});
