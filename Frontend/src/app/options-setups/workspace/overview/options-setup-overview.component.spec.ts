import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakeService } from '../../../components/options-backtest/testing/fake-service';
import { EXAMPLE_NO_REENTRY, exampleBody, servedExamples, servedSaved } from '../../../components/options-backtest/testing/fixtures';
import { typeInto } from '../../../components/options-backtest/testing/page-actions';
import { approvalOf, GOLDEN_ID, indexOf, runOf, savedName, STUDY_ID, studyOf } from '../../testing/fixtures';
import { renderDefine, renderTab } from '../../testing/render-workspace';
import type { OptionsBotOfFleet } from './options-setup-bots.service';

const BOT: OptionsBotOfFleet = {
  sid: 'sid-1',
  label: 'Options bot',
  world: 'Paper',
  status: 'Running',
  group: 'running',
  setupSha256: '1'.repeat(64),
  goldenSetupId: GOLDEN_ID,
  route: ['/brokers', 'alpaca', 'clerks', 'clrk_1', 'accounts', 'PA9', 'bots', 'sid-1'],
};

/** The library the page's own read finds: the shipped examples and a few saved names. The evidence read is the spec's. */
const library = () => fakeService(vi, { list: vi.fn(async () => [...servedExamples(), ...['Alpha', 'Beta', 'Gamma'].map((name) => servedSaved(name, exampleBody()))]) });
/** "Alpha" and "Beta" hold the content `1`, unless a spec says what they hold. */
const overview = (name: string, options: Parameters<typeof renderTab>[3]) =>
  renderTab(library(), name, 'overview', { saved: [savedName('Alpha', '1'), savedName('Beta', '1')], ...options });
const block = (name: string) => screen.getByRole('region', { name });
const text = (element: HTMLElement) => element.textContent?.replace(/\s+/g, ' ') ?? '';

describe('Overview: what is recorded for the content this setup is saved as', () => {
  const index = () =>
    indexOf({
      runs: [runOf(5, '1', { recorded_name: 'Alpha', created_at_ms: 1_791_800_000_005 }), runOf(9, '1', { net_profit_usd: '-80.00', created_at_ms: 1_791_800_000_009 }), runOf(6, '2')],
      studies: [studyOf('1')],
      approvals: [approvalOf('1', { study_id: STUDY_ID, origin: 'search', source_run_id: 7 }), approvalOf('2', { id: `ogs_${'c'.repeat(32)}` })],
    });

  it('shows the latest approved version with how it was approved and where from, as links to its own evidence', async () => {
    await overview('Alpha', { index: index(), bots: { kind: 'known', bots: [] } });

    const approved = await screen.findByRole('region', { name: 'Approved versions of this content' });
    const version = within(approved).getByRole('article');

    expect(text(version)).toContain('Latest.');
    expect(text(version)).toContain('Approved from a golden search');
    expect(within(version).getByRole('link', { name: `Open exact version ${GOLDEN_ID}` }).getAttribute('href')).toBe(`/options-setups/evidence/golden/${GOLDEN_ID}`);
    expect(within(version).getByRole('link', { name: 'run 7' }).getAttribute('href')).toBe('/options-setups/evidence/run/7');
    expect(within(version).getByRole('link', { name: `search ${STUDY_ID}` }).getAttribute('href')).toBe(`/options-setups/evidence/study/${STUDY_ID}`);
    expect(text(version)).toContain('Ready');
    // The other approval froze different content, and is not listed here.
    expect(within(approved).queryByText(new RegExp('c'.repeat(32)))).toBeNull();
  });

  it('shows the last backtest of exactly this content, newest first, with the others counted', async () => {
    await overview('Alpha', { index: index(), bots: { kind: 'known', bots: [] } });

    const backtest = await screen.findByRole('region', { name: 'Last backtest of this content' });
    const rows = within(backtest).getAllByText(/^Run \d+$/);

    expect(rows.map((row) => row.textContent)).toEqual(['Run 9', 'Run 5']);
    expect(text(backtest)).toContain('Latest. Run 9');
    expect(text(backtest)).toContain('net -$80.00');
    expect(text(backtest)).toContain('recorded as “Alpha”');
    // Run 6 froze other content, whatever it was recorded as.
    expect(within(backtest).queryByText('Run 6')).toBeNull();
  });

  it('lists the searches that began from exactly this content', async () => {
    await overview('Alpha', { index: index(), bots: { kind: 'known', bots: [] } });

    const searches = await screen.findByRole('region', { name: 'Searches that began from this content' });

    expect(within(searches).getByRole('link', { name: `Search ${STUDY_ID}` }).getAttribute('href')).toBe(`/options-setups/evidence/study/${STUDY_ID}`);
  });

  it('says another name holds identical content, shares this evidence, and owns none of it', async () => {
    await overview('Alpha', { index: index(), bots: { kind: 'known', bots: [] } });

    const saved = await screen.findByRole('region', { name: 'Saved content' });

    expect(text(saved)).toContain('The same content is also saved as “Beta”');
    expect(text(saved)).toContain('share the evidence below, and none of them owns it');
    expect(within(saved).getByRole('link', { name: '“Beta”' }).getAttribute('href')).toBe('/options-setups/Beta/overview');
  });

  it('says there is none, rather than showing evidence of another content, for a name whose content was edited since', async () => {
    // Alpha was edited: its saved content is now 9, and everything below froze content 1.
    await overview('Alpha', { saved: [savedName('Alpha', '9')], index: indexOf({ runs: [runOf(5, '1', { recorded_name: 'Alpha' })], approvals: [approvalOf('1', { recorded_name: 'Alpha' })] }), bots: { kind: 'known', bots: [] } });

    await screen.findByRole('region', { name: 'Saved content' });

    expect(text(block('Approved versions of this content'))).toContain('No approved version holds this exact content.');
    expect(text(block('Last backtest of this content'))).toContain('No run of this exact content is recorded.');
    expect(text(block('Searches that began from this content'))).toContain('No golden search began from this exact content.');
  });

  it('says a list was cut, so that "none" is never read as "none anywhere"', async () => {
    const cut = indexOf({ runs: [runOf(5, '2')], totals: { runs_total: 1500 } });
    await overview('Alpha', { index: cut, bots: { kind: 'known', bots: [] } });

    await screen.findByRole('region', { name: 'Saved content' });

    expect(text(block('Last backtest of this content'))).toContain('No run of this exact content is recorded, among the newest 1 of 1500.');
  });

  it('says a shipped example cannot be overwritten, and that what is listed was recorded for the content last saved', async () => {
    await overview(EXAMPLE_NO_REENTRY, { saved: [savedName(EXAMPLE_NO_REENTRY, '1', { example: true, saved_at_ms: null })], index: indexOf({}), bots: { kind: 'known', bots: [] } });
    await screen.findByRole('region', { name: 'Saved content' });

    expect(text(block('Saved content'))).toContain('A shipped example, which cannot be overwritten or deleted.');
    expect(text(block('Saved content'))).toContain('Everything below was recorded for exactly the content last saved');
  });
});

describe('Overview: reads once for the workspace', () => {
  it('asks the server for the evidence, the saved setups and the bots once, however the owner moves between the tabs', async () => {
    const read = vi.fn(async () => indexOf({ approvals: [approvalOf('1')] }));
    const bots = vi.fn(async () => ({ kind: 'known' as const, bots: [] }));
    const service = library();
    await renderTab(service, 'Alpha', 'overview', { saved: [savedName('Alpha', '1')], index: read, bots });
    await screen.findByRole('region', { name: 'Saved content' });

    const tab = (name: string) => within(screen.getByRole('navigation', { name: 'Options setup tabs' })).getByRole('link', { name });
    fireEvent.click(tab('Define'));
    await screen.findByRole('heading', { name: 'Entry settings' });
    fireEvent.click(tab('Overview'));
    await screen.findByRole('region', { name: 'Saved content' });
    fireEvent.click(tab('Golden'));
    await screen.findByRole('region', { name: 'Approved versions' });
    fireEvent.click(tab('Overview'));
    await screen.findByRole('region', { name: 'Saved content' });

    expect(read).toHaveBeenCalledTimes(1);
    expect(bots).toHaveBeenCalledTimes(1);
    expect(service.list).toHaveBeenCalledTimes(1);
  });
});

describe('Overview: bots', () => {
  const alpha = () => indexOf({ approvals: [approvalOf('1')] });

  it('lists the bots started from exactly this content, each with the version it pinned', async () => {
    await overview('Alpha', { index: alpha(), bots: { kind: 'known', bots: [BOT, { ...BOT, sid: 'sid-2', setupSha256: '2'.repeat(64), label: 'Another content' }] } });

    const bots = await screen.findByRole('region', { name: 'Bots' });

    expect(within(bots).getByRole('link', { name: 'Options bot' }).getAttribute('href')).toBe('/brokers/alpaca/clerks/clrk_1/accounts/PA9/bots/sid-1');
    expect(text(bots)).toContain('Paper · Running');
    expect(within(bots).getByRole('link', { name: GOLDEN_ID }).getAttribute('href')).toBe(`/options-setups/evidence/golden/${GOLDEN_ID}`);
    expect(within(bots).queryByText('Another content')).toBeNull();
  });

  it('says no bot is running from this content only when every account was read', async () => {
    await overview('Alpha', { index: alpha(), bots: { kind: 'known', bots: [{ ...BOT, setupSha256: '2'.repeat(64) }] } });

    expect(text(await screen.findByRole('region', { name: 'Bots' }))).toContain('No bot is running from this exact content.');
  });

  it('says unknown, never none, when the fleet could not be read in full', async () => {
    await overview('Alpha', { index: alpha(), bots: { kind: 'unknown', reason: 'The account “Live” is not ready, so its bots cannot be listed.' } });

    const bots = await screen.findByRole('region', { name: 'Bots' });

    expect(text(bots)).toContain('Unknown. The account “Live” is not ready, so its bots cannot be listed.');
    expect(text(bots)).not.toContain('No bot');
  });

  it('says unknown when the bots read itself fails', async () => {
    await overview('Alpha', {
      index: alpha(),
      bots: async () => {
        throw new Error('directory down');
      },
    });

    expect(text(await screen.findByRole('region', { name: 'Bots' }))).toContain('Unknown. The bots could not be read.');
  });
});

describe('Overview: when it cannot show what it is for', () => {
  it('says so in place and reads again on request, when the evidence cannot be read', async () => {
    let fail = true;
    const read = vi.fn(async () => {
      if (fail) throw new Error('research store down');
      return indexOf({});
    });
    await overview('Alpha', { index: read, bots: { kind: 'known', bots: [] } });

    const failure = await screen.findByRole('alert');
    expect(failure.textContent).toContain('The evidence recorded for this setup could not be read.');
    fail = false;
    fireEvent.click(within(failure).getByRole('button', { name: 'Read again' }));

    await screen.findByRole('region', { name: 'Saved content' });
  });

  it('says a draft with no saved name has no evidence recorded here, and leads to Define', async () => {
    await renderTab(library(), null, 'overview', { index: indexOf({}), bots: { kind: 'known', bots: [] } });

    expect(text(screen.getByRole('status'))).toContain('This draft has no saved name, so no evidence is recorded for it here.');
    expect(screen.getByRole('link', { name: 'Go to Define' }).getAttribute('href')).toBe('/options-setups/_new/define');
  });

  it('leads to Golden, where a tested run is approved and a golden search is started', async () => {
    await overview('Alpha', { index: indexOf({}), bots: { kind: 'known', bots: [] } });

    const approved = await screen.findByRole('region', { name: 'Approved versions of this content' });

    expect(within(approved).getByRole('link', { name: 'Golden' }).getAttribute('href')).toBe('/options-setups/Alpha/golden');
  });
});

describe('Overview and the draft', () => {
  it('keeps the draft across tabs, lists the evidence of the saved content while the draft is edited, and says the draft has changed', async () => {
    await renderDefine(library(), undefined, {
      setupName: 'Alpha',
      saved: [savedName('Alpha', '1')],
      index: indexOf({ approvals: [approvalOf('1')] }),
      bots: { kind: 'known', bots: [] },
    });
    await waitFor(() => expect((screen.getByLabelText('Setup name') as HTMLInputElement).value).toBe('Alpha'));
    typeInto('Slippage (percent)', '0.9');

    fireEvent.click(screen.getByRole('link', { name: 'Overview' }));

    const saved = await screen.findByRole('region', { name: 'Saved content' });
    expect(text(saved)).toContain('This draft has changed since it was saved as “Alpha”');
    expect(within(block('Approved versions of this content')).getByRole('link', { name: `Open exact version ${GOLDEN_ID}` })).toBeTruthy();
    // Going back, the edit is still there: the tab switch did not reopen the saved setup over it.
    fireEvent.click(screen.getByRole('link', { name: 'Define' }));
    await screen.findByRole('heading', { name: 'Entry settings' });
    expect((screen.getByLabelText('Slippage (percent)') as HTMLInputElement).value).toBe('0.9');
  });
});
