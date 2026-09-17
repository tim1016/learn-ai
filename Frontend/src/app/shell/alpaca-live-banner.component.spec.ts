import { ChangeDetectionStrategy, Component } from '@angular/core';
import { Router, provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';
import axe from 'axe-core';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { provideFleetDirectory, testLane, TEST_ACCOUNT_ID } from '../fleet/fleet-directory-testing';
import type { LaneDescriptor } from '../fleet/fleet-directory.types';
import {
  AlpacaLiveVerdictService,
  UNPOLLED_LANE_STATE,
  type LaneVerdictState,
} from '../services/alpaca-live-verdict.service';
import { AlpacaLiveBannerComponent } from './alpaca-live-banner.component';

function verdict(overrides: Partial<AlpacaLiveVerdict>): AlpacaLiveVerdict {
  return {
    configured_mode: 'paper',
    observed_account_id: 'PA9',
    mode_agreement: 'agreed',
    clerk_authority: 'sqlite',
    clerk_refusal_reason_code: null,
    armed_instance_count: 0,
    envelope_state: 'not_applicable',
    // A paper verdict carries no envelope and no hold, and the server says so
    // with 'not_applicable' on both. The live cases below opt in explicitly.
    envelope_agreement: 'not_applicable',
    shadow_state: 'not_applicable',
    loss_hold: 'not_applicable',
    final_verdict: 'paper',
    headline: 'Paper account PA9 — no real money at risk',
    detail: 'ALPACA_MODE=paper.',
    observed_at_ms: 1_700_000_000_000,
    ...overrides,
  };
}

/** Stands in for whatever page the operator is on: the badge only reads the
 * URL, never what is rendered under it. */
@Component({ selector: 'app-anywhere', template: '', changeDetection: ChangeDetectionStrategy.OnPush })
class AnywhereComponent {}

async function renderWith(
  lane: LaneDescriptor | null,
  state: LaneVerdictState,
  siblingLanes: readonly LaneDescriptor[] = [],
) {
  const stateFor = (clerkId: string): LaneVerdictState =>
    clerkId === lane?.clerk_id ? state : UNPOLLED_LANE_STATE;
  return render(AlpacaLiveBannerComponent, {
    inputs: { lane },
    providers: [
      { provide: AlpacaLiveVerdictService, useValue: { stateFor } },
      // Siblings for disambiguation now come from `FleetDirectoryService`
      // (`lanesOf`), not a prop — an explicit, empty-by-default directory so
      // a test that doesn't care about collisions never accidentally gets
      // one from `provideFleetDirectory()`'s own default fixture lane.
      provideFleetDirectory({ observed_at_ms: 1, clerks: [...siblingLanes] }),
      // The badge is a link into its account's workspace (#2187); where it
      // leads depends on the URL the shell is currently on. The catch-all
      // route exists so a spec can *stand* on a URL — the badge reads the
      // router's URL, which only moves when a navigation actually matches.
      provideRouter([{ path: '**', component: AnywhereComponent }]),
    ],
  });
}

/** Render a badge as if the operator were standing on `url` — the badge reads
 * the current URL to decide whether it is switching accounts inside a
 * workspace or opening one from outside. */
async function renderAt(
  url: string,
  lane: LaneDescriptor,
  state: LaneVerdictState = { verdict: verdict({}), lastError: null },
) {
  const view = await renderWith(lane, state);
  const router = view.fixture.debugElement.injector.get(Router);
  await router.navigateByUrl(url);
  await view.fixture.whenStable();
  return view;
}

/** Another account's workspace — where a badge is a switch rather than an
 * opening. */
const LIVE_WORKSPACE = '/brokers/alpaca/clerks/clrk_live/accounts/LIVE9';

const PAPER_LANE = testLane({ clerk_id: 'clrk_paper', broker: 'alpaca', display_label: 'Paper' });
const LIVE_LANE = testLane({ clerk_id: 'clrk_live', broker: 'alpaca', display_label: 'Live' });

describe('AlpacaLiveBannerComponent', () => {
  it('warns loudly before the first read for this lane, never renders nothing', async () => {
    await renderWith(PAPER_LANE, { verdict: null, lastError: null });

    const status = screen.getByRole('status');
    expect(status.className).toContain('is-undetermined');
    expect(status.textContent).toContain('Paper');
    expect(status.textContent).toContain('assume real money');
  });

  it('warns loudly when there is no lane at all, because the roster itself is unknown', async () => {
    await renderWith(null, UNPOLLED_LANE_STATE);

    const status = screen.getByRole('status');
    expect(status.className).toContain('is-undetermined');
    expect(status.textContent).toContain('Alpaca lanes unknown');
    expect(status.textContent).toContain('assume real money');
  });

  it('renders paper mode as a compact Paper money chip labeled with its lane', async () => {
    await renderWith(PAPER_LANE, { verdict: verdict({}), lastError: null });
    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Paper');
    expect(status.textContent).toContain('Paper money');
    expect(status.getAttribute('title')).toBe('ALPACA_MODE=paper.');
    expect(status.className).toContain('is-paper');

    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it('renders a live-unarmed account loudly with the lane label and armed count, never its account number', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        envelope_state: 'configured_unsealed',
        envelope_agreement: 'unsealed',
        shadow_state: 'none',
        final_verdict: 'live-unarmed',
        headline: 'LIVE account 9LIVE0001 — real money, no instance armed',
        detail: 'Every order path refuses.',
      }),
      lastError: null,
    });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-live-unarmed');
    expect(status.textContent).toContain('Live');
    expect(status.textContent).toContain('0 armed');
    // The account number names the account but guards nothing here — the lane
    // label already names it, and the number belongs only on Configuration and
    // in the confirmation of a consequential action (ADR 0064; #2188).
    expect(status.textContent).not.toContain('9LIVE0001');
  });

  it("says Shadow, not an account number, when the clerk holds the no-submit Shadow authority", async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        clerk_authority: 'shadow',
        envelope_state: 'sealed',
        envelope_agreement: 'agreed',
        shadow_state: 'complete',
        armed_instance_count: 2,
        final_verdict: 'live-armed',
        headline: 'LIVE account 9LIVE0001 — 2 instances armed, shadowing',
        detail: 'The shadow port synthesizes fills and submits nothing.',
      }),
      lastError: null,
    });

    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Live');
    expect(status.textContent).toContain('Shadow');
    expect(status.textContent).toContain('2 armed');
    expect(status.textContent).not.toContain('9LIVE0001');
  });

  it('says nothing about Shadow when the clerk holds ordinary SQLite authority', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        clerk_authority: 'sqlite',
        envelope_state: 'configured_unsealed',
        envelope_agreement: 'unsealed',
        shadow_state: 'none',
        final_verdict: 'live-unarmed',
        headline: 'LIVE account 9LIVE0001 — real money, no instance armed',
        detail: 'Every order path refuses.',
      }),
      lastError: null,
    });

    expect(screen.getByRole('status').textContent).not.toContain('Shadow');
  });

  it('shows the loss hold on a live account when held', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        envelope_state: 'configured_unsealed',
        envelope_agreement: 'unsealed',
        shadow_state: 'none',
        final_verdict: 'live-unarmed',
        headline: 'LIVE account 9LIVE0001 — real money, no instance armed',
        detail: 'Every order path refuses.',
        loss_hold: 'held',
      }),
      lastError: null,
    });
    expect(screen.getByRole('status').textContent).toContain('loss hold');
  });

  it('shows nothing extra on a live account when the loss hold is clear', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        envelope_state: 'configured_unsealed',
        envelope_agreement: 'unsealed',
        shadow_state: 'none',
        final_verdict: 'live-unarmed',
        headline: 'LIVE account 9LIVE0001 — real money, no instance armed',
        detail: 'Every order path refuses.',
        loss_hold: 'clear',
      }),
      lastError: null,
    });
    expect(screen.getByRole('status').textContent).not.toContain('loss hold');
  });

  it('renders a server-reported unknown verdict as a loud warning that says assume real money', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: null,
        mode_agreement: 'disagreed',
        clerk_refusal_reason_code: 'LIVE_MODE_DISAGREEMENT',
        final_verdict: 'unknown',
        headline: 'Live mode configured — account state unknown',
        detail: 'the configured mode and the observed account disagree',
      }),
      lastError: null,
    });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-undetermined');
    expect(status.className).not.toContain('is-unknown');
    expect(status.textContent).toContain('assume real money');
    expect(status.textContent).toContain('Live Mode Disagreement');

    // The amber treatment's own markup, never axe-run before this round.
    // `color-contrast` stays off because jsdom computes no layout or cascade,
    // so axe cannot evaluate it here — asserting it would be a check that
    // cannot fail. The remaining rules do apply to this path.
    const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
    expect(results.violations).toEqual([]);
  });

  it('says Shadow on an undetermined verdict when the clerk holds the no-submit Shadow authority', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: null,
        mode_agreement: 'disagreed',
        clerk_authority: 'shadow',
        clerk_refusal_reason_code: 'LIVE_MODE_DISAGREEMENT',
        final_verdict: 'unknown',
        headline: 'Live mode configured — account state unknown',
        detail: 'the configured mode and the observed account disagree',
      }),
      lastError: null,
    });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-undetermined');
    expect(status.textContent).toContain('Shadow');
    expect(status.textContent).toContain('assume real money');
  });

  it('keeps an explicit loud warning on screen when the last read failed, never a grey unknown', async () => {
    await renderWith(PAPER_LANE, { verdict: null, lastError: new Error('down') });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-undetermined');
    expect(status.className).not.toContain('is-unknown');
    expect(status.textContent).toContain('Mode unavailable');
    expect(status.textContent).toContain('assume real money');
  });

  it.each([
    [
      'the last read failed',
      PAPER_LANE,
      { verdict: null, lastError: new Error('down') } satisfies LaneVerdictState,
    ],
    [
      'no read has completed yet',
      PAPER_LANE,
      UNPOLLED_LANE_STATE,
    ],
    [
      'the server itself reports unknown',
      LIVE_LANE,
      {
        verdict: verdict({
          configured_mode: 'live',
          observed_account_id: null,
          final_verdict: 'unknown',
          headline: 'Live mode configured — account state unknown',
        }),
        lastError: null,
      } satisfies LaneVerdictState,
    ],
    ['there is no lane at all', null, UNPOLLED_LANE_STATE],
  ])(
    'carries the real-money assumption in the ACCESSIBLE NAME when %s (WCAG 1.4.1)',
    async (_cause, lane, state) => {
      await renderWith(lane, state);

      // `aria-label` wins the accessible-name computation, so a screen reader
      // announcing this live region by name must still hear the assumption —
      // it cannot survive in the visible text and the amber alone.
      const name = screen.getByRole('status').getAttribute('aria-label');
      expect(name).toContain('Assume real money');
      if (lane) expect(name).toContain(lane.display_label);
    },
  );

  it('renders a live-armed account in the loudest treatment with the armed count', async () => {
    await renderWith(LIVE_LANE, {
      verdict: verdict({
        configured_mode: 'live',
        observed_account_id: '9LIVE0001',
        armed_instance_count: 1,
        envelope_state: 'sealed',
        envelope_agreement: 'agreed',
        shadow_state: 'complete',
        final_verdict: 'live-armed',
        headline: 'LIVE account 9LIVE0001 — 1 instance armed, nothing submitted yet',
        detail: 'No path submits a real-money order in this slice.',
      }),
      lastError: null,
    });
    const status = screen.getByRole('status');
    expect(status.className).toContain('is-live-armed');
    expect(status.textContent).toContain('1 armed');
    expect(status.textContent).not.toContain('9LIVE0001');
  });

  it("shows the lane's account nickname instead of its raw label when one is set", async () => {
    const named = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Strategy lab' },
    });
    await renderWith(named, { verdict: verdict({}), lastError: null });

    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Strategy lab');
    expect(status.querySelector('.alpaca-banner__lane')?.textContent).toBe('Strategy lab');
  });

  it("shows this lane's own label beside its name when another lane shares it (ADR 0064 Decision 5)", async () => {
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
    await renderWith(paper, { verdict: verdict({}), lastError: null }, [paper, live]);

    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Strategy lab');
    expect(status.textContent).toContain('(Paper)');
  });

  // Two lanes sharing a display name are a deliberately supported state
  // (ADR 0064 Decision 5), not an edge case: without carrying the
  // disambiguator into `aria-label` too, both badges would announce
  // identically to a screen reader (WCAG 4.1.2 / axe landmark-unique
  // territory), even though their visible pills already read differently.
  it("carries the disambiguator into the paper lane's accessible name, not only its visible pill", async () => {
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
    await renderWith(paper, { verdict: verdict({}), lastError: null }, [paper, live]);

    expect(screen.getByRole('status').getAttribute('aria-label')).toContain(
      'Strategy lab (Paper)',
    );
  });

  it("carries the disambiguator into the live lane's accessible name, not only its visible pill", async () => {
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
    await renderWith(live, { verdict: verdict({ observed_account_id: 'PA9' }), lastError: null }, [
      paper,
      live,
    ]);

    expect(screen.getByRole('status').getAttribute('aria-label')).toContain(
      'strategy lab (Live)',
    );
  });

  it('carries the disambiguator into the accessible name on the undetermined path too (not just the verdict path)', async () => {
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
    await renderWith(paper, { verdict: null, lastError: new Error('down') }, [paper, live]);

    const name = screen.getByRole('status').getAttribute('aria-label');
    expect(name).toContain('Strategy lab (Paper)');
    expect(name).toContain('Assume real money');
  });

  it('never refuses a duplicate name, it only disambiguates', async () => {
    const paper = testLane({
      clerk_id: 'clrk_paper',
      display_label: 'Paper',
      provider_summary: { account_nickname: 'Solo' },
    });
    await renderWith(paper, { verdict: verdict({}), lastError: null }, [paper]);

    const status = screen.getByRole('status');
    expect(status.textContent).toContain('Solo');
    expect(status.textContent).not.toContain('(Paper)');
  });

  describe('as the way to its account (ADR 0064 Decision 3)', () => {
    it("opens the account's Overview from anywhere outside a workspace", async () => {
      await renderAt('/data-lab', PAPER_LANE);

      expect(screen.getByRole('link').getAttribute('href')).toBe(
        `/brokers/alpaca/clerks/clrk_paper/accounts/${TEST_ACCOUNT_ID}`,
      );
    });

    it.each([
      ['', ''],
      ['/bots', '/bots'],
      ['/gallery', '/gallery'],
    ])('keeps the %s tab when switching from another account inside a workspace', async (
      tab,
      expected,
    ) => {
      await renderAt(`${LIVE_WORKSPACE}${tab}`, PAPER_LANE);

      expect(screen.getByRole('link').getAttribute('href')).toBe(
        `/brokers/alpaca/clerks/clrk_paper/accounts/${TEST_ACCOUNT_ID}${expected}`,
      );
    });

    it("lands on the chosen account's Bots from a bot's own page", async () => {
      // The chosen account need not run that bot, so the bot's page is never
      // carried across — the same rule the in-workspace switcher follows.
      await renderAt(`${LIVE_WORKSPACE}/bots/sid-1?from=gallery`, PAPER_LANE);

      expect(screen.getByRole('link').getAttribute('href')).toBe(
        `/brokers/alpaca/clerks/clrk_paper/accounts/${TEST_ACCOUNT_ID}/bots`,
      );
    });

    it('carries the lens perspective across, exactly as the in-workspace switcher does', async () => {
      await renderAt(`${LIVE_WORKSPACE}/bots?lens=operator`, PAPER_LANE);

      expect(screen.getByRole('link').getAttribute('href')).toBe(
        `/brokers/alpaca/clerks/clrk_paper/accounts/${TEST_ACCOUNT_ID}/bots?lens=operator`,
      );
    });

    it('leaves an open Deploy drawer behind rather than retargeting it', async () => {
      await renderAt(`${LIVE_WORKSPACE}?deploy=`, PAPER_LANE);

      expect(screen.getByRole('link').getAttribute('href')).toBe(
        `/brokers/alpaca/clerks/clrk_paper/accounts/${TEST_ACCOUNT_ID}`,
      );
    });

    it("opens an unconfirmed account's Configuration, the one tab its lane can serve", async () => {
      const unbound = testLane({
        clerk_id: 'clrk_paper',
        display_label: 'Paper',
        provider_summary: { ...testLane().provider_summary, confirmed_account_id: null },
      });
      await renderAt('/data-lab', unbound);

      expect(screen.getByRole('link').getAttribute('href')).toBe(
        '/brokers/alpaca/clerks/clrk_paper/configuration',
      );
    });

    it('is a link that does not leave the badge — the warning is still the status region', async () => {
      // The link wraps the live region rather than replacing it: one element
      // cannot be both, and the real-money assumption has to keep announcing
      // as a status with its own accessible name (WCAG 1.4.1).
      await renderAt('/data-lab', PAPER_LANE, { verdict: null, lastError: new Error('down') });

      const status = screen.getByRole('status');
      expect(status.className).toContain('is-undetermined');
      expect(status.getAttribute('aria-label')).toContain('Assume real money');
      expect(screen.getByRole('link').contains(status)).toBe(true);
      // The link inherits that same accessible name from the region it wraps,
      // so tabbing onto the badge cannot hear less than reading it does.
      expect(screen.getByRole('link').getAttribute('aria-label')).toBeNull();
      expect(screen.getByRole('link', { name: /Assume real money/ })).toBeTruthy();

      const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
      expect(results.violations).toEqual([]);
    });

    it('is not a link when there is no lane to open, rather than a link to nowhere', async () => {
      await renderWith(null, UNPOLLED_LANE_STATE);

      expect(screen.queryByRole('link')).toBeNull();
      expect(screen.getByRole('status').textContent).toContain('Alpaca lanes unknown');
    });
  });
});
