import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { fakeVerdictState } from '../../../../testing/alpaca-live-verdict-fixtures';
import { provideFleetDirectory, testLane, TEST_ACCOUNT_ID, TEST_CLERK_ID } from '../../../../fleet/fleet-directory-testing';
import {
  verdictModeChip,
  UNPOLLED_LANE_STATE,
} from '../../../../services/alpaca-live-verdict.service';
import { AlpacaLaneDirectoryComponent } from './alpaca-lane-directory.component';

describe('AlpacaLaneDirectoryComponent', () => {
  it('keeps healthy lane actions available while making a failed lane explicit', async () => {
    const healthy = testLane();
    const failed = testLane({
      clerk_id: 'clerk-offline',
      display_label: 'Live',
      lifecycle_state: 'unreachable',
      provider_summary: {
        ...healthy.provider_summary,
        confirmed_account_id: 'live-account',
      },
    });
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({ observed_at_ms: 1, clerks: [healthy, failed] }),
      ],
    });

    expect(screen.getAllByText('Paper')).not.toHaveLength(0);
    expect(screen.getByText('Live')).not.toBeNull();
    expect(screen.getByText(/other healthy lanes remain available/i)).not.toBeNull();
    expect(screen.getAllByRole('link', { name: 'Account details' })).toHaveLength(1);
    expect(screen.getAllByRole('link', { name: 'Deploy' })).toHaveLength(1);
    expect(screen.getAllByRole('link', { name: 'Configuration' })).toHaveLength(2);
  });

  it('links a serving lane to its canonical surface URL, never to another lane', async () => {
    await render(AlpacaLaneDirectoryComponent, {
      providers: [provideRouter([]), provideFleetDirectory()],
    });

    const bots = screen.getByRole('link', { name: 'Bot roster' });
    expect(bots.getAttribute('href')).toBe(
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}/bots`,
    );
    expect(screen.getByRole('link', { name: 'Gallery' }).getAttribute('href')).toBe(
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}/gallery`,
    );
    const accountUrl = `/brokers/alpaca/clerks/${TEST_CLERK_ID}/accounts/${TEST_ACCOUNT_ID}`;
    expect(screen.getByRole('link', { name: 'Account details' }).getAttribute('href')).toBe(accountUrl);
    expect(screen.getByRole('link', { name: 'Trader view' }).getAttribute('href')).toBe(`${accountUrl}?lens=trader`);
    expect(screen.getByRole('link', { name: 'Operator view' }).getAttribute('href')).toBe(`${accountUrl}?lens=operator`);
    expect(screen.getByRole('link', { name: 'Transaction history' }).getAttribute('href')).toBe(
      `${accountUrl}?lens=operator#account-desk-transaction-history`,
    );
  });

  it('keeps an unavailable lane selectable through its clerk-only surface route', async () => {
    const healthy = testLane();
    const failed = testLane({
      clerk_id: 'clerk-offline',
      lifecycle_state: 'unreachable',
      provider_summary: {
        ...healthy.provider_summary,
        confirmed_account_id: 'live-account',
      },
    });
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({ observed_at_ms: 1, clerks: [healthy, failed] }),
      ],
    });

    const botsLinks = screen.getAllByRole('link', { name: 'Bot roster' }).sort(
      (left, right) =>
        (left.getAttribute('href') ?? '').length - (right.getAttribute('href') ?? '').length,
    );
    // The unavailable lane's Bots link stays present, points at its own
    // clerk-only explanation, and reads as not-yet-openable.
    expect(botsLinks[0].getAttribute('href')).toBe('/brokers/alpaca/clerks/clerk-offline/bots');
    expect(botsLinks[0].classList.contains('lane-card__link--blocked')).toBe(true);
    expect(screen.getAllByRole('link', { name: 'Gallery' }).some((link) =>
      link.getAttribute('href') === '/brokers/alpaca/clerks/clerk-offline/gallery',
    )).toBe(true);
  });

  it('routes surface links for a lane without the capability to its clerk-only route', async () => {
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [testLane({ capabilities: ['account_read', 'configuration_manage'] })],
        }),
      ],
    });

    expect(screen.getByRole('link', { name: 'Account details' }).getAttribute('href')).toContain('/accounts/');
    expect(screen.queryByRole('link', { name: 'Deploy' })).toBeNull();
    expect(screen.getByRole('link', { name: 'Bot roster' }).getAttribute('href')).toBe(
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/bots`,
    );
    expect(screen.getByRole('link', { name: 'Gallery' }).getAttribute('href')).toBe(
      `/brokers/alpaca/clerks/${TEST_CLERK_ID}/gallery`,
    );
  });

  it("renders the lane's authority state through receiptLabel (#2102)", async () => {
    // `authority_state` (records.py) reaches the browser but rendered nowhere
    // before #2102 — it is a raw backend identifier (like `endpoint_mode`
    // above), so it belongs through the shared pipe, not as literal prose.
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({
          observed_at_ms: 1,
          clerks: [testLane({ provider_summary: { authority_state: 'real_live' } })],
        }),
      ],
    });

    expect(screen.getByText('Real Live')).toBeTruthy();
  });

  it('renders one mode chip per lane, loud and undetermined before any verdict lands', async () => {
    const healthy = testLane();
    const failed = testLane({
      clerk_id: 'clerk-offline',
      lifecycle_state: 'unreachable',
      provider_summary: {
        ...healthy.provider_summary,
        confirmed_account_id: 'live-account',
      },
    });
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({ observed_at_ms: 1, clerks: [healthy, failed] }),
      ],
    });

    const chips = screen.getAllByText('Mode unknown — assume real money');
    expect(chips).toHaveLength(2);
    for (const chip of chips) {
      expect(chip.classList.contains('lane-mode-chip--undetermined')).toBe(true);
    }
  });

  it('renders the bots chooser heading and lead sentence without selecting a lane', async () => {
    await render(AlpacaLaneDirectoryComponent, {
      inputs: { surface: 'bots' },
      providers: [provideRouter([]), provideFleetDirectory()],
    });

    expect(screen.getByText('Choose an account — Bots roster')).toBeTruthy();
    expect(screen.getByText(/no lane is selected automatically/i)).toBeTruthy();
  });

  it('makes a global Deploy intent an explicit lane-selection step', async () => {
    await render(AlpacaLaneDirectoryComponent, {
      inputs: { deployIntent: true },
      providers: [provideRouter([]), provideFleetDirectory()],
    });

    expect(screen.getByText(/choose a ready Paper or Live account below to deploy/i)).toBeTruthy();
    // The `href` alone proves only that the link is addressed at a lane — it
    // cannot see whether the destination still opens the drawer. That is
    // pinned by following this link for real in
    // `alpaca-account-workspace.component.spec.ts`.
    expect(screen.getByRole('link', { name: 'Deploy' }).getAttribute('href')).toContain(
      '/brokers/alpaca/clerks/',
    );
  });

  it("shows a lane's account nickname instead of its raw label (ADR 0064 Decision 5)", async () => {
    const named = testLane({
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({ observed_at_ms: 1, clerks: [named] }),
      ],
    });

    expect(screen.getByText('Strategy lab')).toBeTruthy();
    expect(screen.queryByText('Paper')).toBeNull();
  });

  it("shows this lane's own label beside a nickname shared with another lane, never refusing the duplicate", async () => {
    const paper = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    const live = testLane({
      clerk_id: 'clrk_live',
      display_label: 'Live',
      provider_summary: { account_nickname: '  strategy lab  ' },
    });
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({ observed_at_ms: 1, clerks: [paper, live] }),
      ],
    });

    expect(screen.getAllByText('(Paper)')).toHaveLength(1);
    expect(screen.getAllByText('(Live)')).toHaveLength(1);
  });

  it("carries the disambiguator into each colliding lane's surfaces-nav accessible name, not just its visible text", async () => {
    // #2182 follow-up: two lanes sharing a display name must not render two
    // `nav` landmarks with an identical accessible name (axe
    // landmark-unique-adjacent territory) — `aria-label` has to include the
    // same disambiguator the visible pill shows.
    const paper = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    const live = testLane({
      clerk_id: 'clrk_live',
      display_label: 'Live',
      provider_summary: { account_nickname: '  strategy lab  ' },
    });
    await render(AlpacaLaneDirectoryComponent, {
      providers: [
        provideRouter([]),
        provideFleetDirectory({ observed_at_ms: 1, clerks: [paper, live] }),
      ],
    });

    expect(
      screen.getByRole('navigation', { name: 'Strategy lab (Paper) surfaces' }),
    ).toBeTruthy();
    expect(
      screen.getByRole('navigation', { name: 'strategy lab (Live) surfaces' }),
    ).toBeTruthy();
  });
});

describe('verdictModeChip', () => {
  const UNDETERMINED = {
    tone: 'undetermined',
    mode: 'Mode unknown — assume real money',
    armedCount: null,
    shadow: false,
  };

  it('reads the verdict through the shared pill vocabulary', () => {
    expect(verdictModeChip(fakeVerdictState('paper'))).toEqual({
      tone: 'paper',
      mode: 'Paper money',
      armedCount: null,
      shadow: false,
    });
    expect(verdictModeChip(fakeVerdictState('live-unarmed'))).toEqual({
      tone: 'live',
      mode: 'Live',
      armedCount: 0,
      shadow: false,
    });
    expect(verdictModeChip(fakeVerdictState('live-armed', { armed_instance_count: 3 }))).toEqual({
      tone: 'live',
      mode: 'Live',
      armedCount: 3,
      shadow: false,
    });
  });

  it('carries the server-declared Shadow authority beside the mode, never instead of it', () => {
    // ADR 0059 D2: a shadowing lane still reads the live account, so it is
    // not a third mode — it is a live lane whose submissions go nowhere.
    expect(verdictModeChip(fakeVerdictState('live-unarmed', { clerk_authority: 'shadow' }))).toEqual({
      tone: 'live',
      mode: 'Live',
      armedCount: 0,
      shadow: true,
    });
  });

  it('renders unread, failed, and server-unknown modes as the loud undetermined chip', () => {
    expect(verdictModeChip(UNPOLLED_LANE_STATE)).toEqual(UNDETERMINED);
    expect(verdictModeChip({ verdict: null, lastError: new Error('down') })).toEqual(UNDETERMINED);
    expect(verdictModeChip(fakeVerdictState('unknown'))).toEqual(UNDETERMINED);
  });
});
