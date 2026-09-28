import { signal } from '@angular/core';
import { render, screen, within } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';
import { resourceTarget } from '../../../fleet/resource-target';
import { BrokerV2PanelService, type DeploymentBudgetView } from '../v2-panel/lib/broker-v2-panel.service';
import { DeploymentBudgetComponent } from './deployment-budget.component';

const TARGET = resourceTarget('alpaca', 'clrk_spec', { accountId: 'PA9', bindingGeneration: 3, routingEpoch: 4 });

const RUNNING: DeploymentBudgetView = {
  state: 'ready', headline: 'Holding its position', detail: 'Its next entry waits until this position is sold.',
  strategy_instance_id: 'spy-ema-a', world: 'real_paper', entry_eligible: false,
  statement: [
    { label: 'Budget set aside at deploy', amount_usd: '1000.00' },
    { label: 'Realized gains and losses', amount_usd: '12.40' },
    { label: 'Fees', amount_usd: '-0.03' },
    { label: 'Balance', amount_usd: '1012.37', total: true },
    { label: 'In shares, at cost', amount_usd: '764.71' },
    { label: 'Waiting in entry orders', amount_usd: '0.00' },
    { label: 'Free to trade', amount_usd: '247.66' },
    { label: 'Short of its next entry', amount_usd: '0.00' },
  ],
  parts: { in_shares_usd: '764.71', in_shares_bps: 7554, pending_usd: '0.00', pending_bps: 0, free_usd: '247.66', free_bps: 2446 },
  note: 'The budget limits new entries. Market fills and losses can go past it.',
  observed_at_ms: 1_800_000_000_000,
};

const STOPPED: DeploymentBudgetView = {
  state: 'ready', headline: 'Stopped · still holds shares',
  detail: 'Its free budget was released when it stopped. The money in its shares comes back when they are sold.',
  strategy_instance_id: 'spy-ema-b', world: 'real_paper', entry_eligible: false,
  statement: [
    { label: 'Budget set aside at deploy', amount_usd: '1000.00' },
    { label: 'Realized gains and losses', amount_usd: '-10.00' },
    { label: 'Fees', amount_usd: '-0.03' },
    { label: 'Balance', amount_usd: '989.97', total: true },
    { label: 'Released at stop', amount_usd: '319.54' },
    { label: 'Still in shares, at cost', amount_usd: '670.43' },
    { label: 'Waiting on orders, fills or fees', amount_usd: '0.00' },
  ],
  parts: { in_shares_usd: '670.43', in_shares_bps: 10000, pending_usd: '0.00', pending_bps: 0, free_usd: '0.00', free_bps: 0 },
  note: null,
  observed_at_ms: 1_800_000_000_000,
};

const UNAVAILABLE: DeploymentBudgetView = {
  state: 'unavailable', headline: "This bot's money is unavailable",
  detail: 'Wait for fresh account cash and risk evidence. The original commitment is retained.',
  strategy_instance_id: 'spy-ema-a', world: 'real_paper', committed_usd: '1000.00',
};

const LEGACY: DeploymentBudgetView = {
  state: 'legacy', headline: 'This bot has no budget',
  detail: 'This earlier deployment has no budget. Stop and reconcile it, then review a fresh Deploy.',
  strategy_instance_id: 'spy-ema-a', world: 'real_paper',
};

interface CardInputs {
  readonly running?: boolean;
  readonly holdsShares?: boolean;
  readonly openPnl?: number | null;
}

/** Rendered where the bot page puts it — inside a landmark — with the page shell's bindings. */
async function renderCard(getBudget: ReturnType<typeof vi.fn>, inputs: CardInputs = {}) {
  const revision = signal(1);
  const result = await render(
    `<main aria-label="Bot"><app-deployment-budget [target]="target" strategyInstanceId="spy-ema-a" [revision]="revision()"
      [running]="running" [holdsShares]="holdsShares" [openPnl]="openPnl" /></main>`,
    {
      imports: [DeploymentBudgetComponent],
      componentProperties: {
        target: TARGET, revision, running: inputs.running ?? true,
        holdsShares: inputs.holdsShares ?? false, openPnl: inputs.openPnl ?? null,
      },
      providers: [{ provide: BrokerV2PanelService, useValue: { getBudget } }],
    },
  );
  return { ...result, revision };
}

function statementRows(): [string, string, boolean][] {
  const card = screen.getByRole('region', { name: "This bot's money" });
  const list = card.querySelector('dl');
  if (list === null) throw new Error('the statement is not rendered');
  return Array.from(list.querySelectorAll('.deployment-budget__line')).map((row) => [
    row.querySelector('dt')?.textContent?.trim() ?? '',
    row.querySelector('dd')?.textContent?.trim() ?? '',
    row.classList.contains('deployment-budget__line--total'),
  ]);
}

function slices(container: Element): HTMLElement[] {
  return Array.from(container.querySelectorAll<HTMLElement>('.money-bar__track > .money-bar__slice'));
}

function parts(container: Element): [string, string][] {
  return Array.from(container.querySelectorAll<HTMLElement>('.money-bar__part'))
    .map((part) => [part.className, part.style.flexGrow]);
}

async function expectAxeClean(): Promise<void> {
  const results = await axe.run(document.body, { rules: { 'color-contrast': { enabled: false } } });
  expect(results.violations).toEqual([]);
}

describe("This bot's money", () => {
  it('shows a running bot’s statement verbatim, in order, with Balance as its total', async () => {
    const { container } = await renderCard(vi.fn().mockResolvedValue(RUNNING));

    await screen.findByText('Holding its position');
    expect(screen.getByText('Its next entry waits until this position is sold.')).toBeTruthy();
    expect(statementRows()).toEqual([
      ['Budget set aside at deploy', '$1,000.00', false],
      ['Realized gains and losses', '$12.40', false],
      ['Fees', '-$0.03', false],
      ['Balance', '$1,012.37', true],
      ['In shares, at cost', '$764.71', false],
      ['Waiting in entry orders', '$0.00', false],
      ['Free to trade', '$247.66', false],
      ['Short of its next entry', '$0.00', false],
    ]);
    expect(screen.getByText('The budget limits new entries. Market fills and losses can go past it.')).toBeTruthy();
    expect(container.querySelector('app-timestamp-display')).not.toBeNull();
    expect(screen.queryByText(/Budget (and|or) account risk/)).toBeNull();
    expect(screen.queryByText(/Stopped does not mean flat/)).toBeNull();
    expect(screen.queryByText('Paper')).toBeNull();
  });

  it('draws the running slice as its whole balance, shaded at the backend’s basis points', async () => {
    const { container } = await renderCard(vi.fn().mockResolvedValue(RUNNING));
    await screen.findByText('Holding its position');

    const drawn = slices(container);
    expect(drawn.map((slice) => [slice.className, slice.style.flexGrow])).toEqual([
      ['money-bar__slice money-bar__slice--bot', '10000'],
    ]);
    expect(parts(container)).toEqual([
      ['money-bar__part money-bar__part--shares', '7554'],
      ['money-bar__part money-bar__part--pending', '0'],
      ['money-bar__part money-bar__part--free', '2446'],
    ]);
    const legend = screen.getByRole('list', { name: "This bot's slice" });
    expect(within(legend).getByRole('listitem').textContent?.replace(/\s+/g, ' ').trim())
      .toBe('Balance $1,012.37 in shares $764.71 · in entry orders $0.00 · free $247.66');
  });

  it('shows a stopped bot’s released and still-held money with a stopped slice', async () => {
    const { container } = await renderCard(vi.fn().mockResolvedValue(STOPPED), { running: false });

    await screen.findByText('Stopped · still holds shares');
    expect(statementRows()).toEqual([
      ['Budget set aside at deploy', '$1,000.00', false],
      ['Realized gains and losses', '-$10.00', false],
      ['Fees', '-$0.03', false],
      ['Balance', '$989.97', true],
      ['Released at stop', '$319.54', false],
      ['Still in shares, at cost', '$670.43', false],
      ['Waiting on orders, fills or fees', '$0.00', false],
    ]);
    expect(slices(container).map((slice) => [slice.className, slice.style.flexGrow])).toEqual([
      ['money-bar__slice money-bar__slice--stopped', '10000'],
    ]);
    expect(parts(container)).toEqual([
      ['money-bar__part money-bar__part--shares', '10000'],
      ['money-bar__part money-bar__part--pending', '0'],
      ['money-bar__part money-bar__part--free', '0'],
    ]);
  });

  it.each([['unavailable', UNAVAILABLE], ['legacy', LEGACY]])('shows only the headline and detail when the money is %s', async (_state, view) => {
    const { container } = await renderCard(vi.fn().mockResolvedValue(view), { holdsShares: true, openPnl: 4.5 });

    await screen.findByText(view.headline);
    expect(screen.getByText(view.detail)).toBeTruthy();
    expect(container.querySelector('app-money-bar')).toBeNull();
    expect(container.querySelector('dl')).toBeNull();
    expect(screen.queryByText('Unknown')).toBeNull();
    expect(screen.queryByText(/Open gain or loss/)).toBeNull();
    expect(screen.queryByText(/Budget (and|or) account risk/)).toBeNull();
  });

  it('notes the open gain or loss on shares apart from the bar while it holds shares', async () => {
    await renderCard(vi.fn().mockResolvedValue(RUNNING), { holdsShares: true, openPnl: -3.25 });

    const note = await screen.findByText(/Open gain or loss on shares:/);
    expect(note.textContent?.replace(/\s+/g, ' ').trim()).toBe('Open gain or loss on shares: -$3.25, counted when sold.');
  });

  it('says there is no current price when the open gain or loss is unknown', async () => {
    await renderCard(vi.fn().mockResolvedValue(RUNNING), { holdsShares: true, openPnl: null });

    expect(await screen.findByText('Open gain or loss on shares: no current price.')).toBeTruthy();
  });

  it('has no open gain or loss note when it holds no shares', async () => {
    await renderCard(vi.fn().mockResolvedValue(RUNNING), { holdsShares: false, openPnl: 7 });

    await screen.findByText('Holding its position');
    expect(screen.queryByText(/Open gain or loss/)).toBeNull();
  });

  it('reads the money again for a new revision', async () => {
    const getBudget = vi.fn().mockResolvedValue(RUNNING);
    const { fixture, revision } = await renderCard(getBudget);
    await screen.findByText('Holding its position');

    revision.set(2);
    await fixture.whenStable();

    expect(getBudget).toHaveBeenCalledTimes(2);
    expect(getBudget).toHaveBeenLastCalledWith(TARGET, 'spy-ema-a');
  });

  it('says so when the money cannot be read, and reads again on Refresh', async () => {
    const getBudget = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(RUNNING);
    await renderCard(getBudget);

    expect((await screen.findByRole('alert')).textContent).toBe("This bot's money could not be read.");
    await userEvent.click(screen.getByRole('button', { name: "Refresh this bot's money" }));

    expect(await screen.findByText('Holding its position')).toBeTruthy();
    expect(getBudget).toHaveBeenCalledTimes(2);
    expect(screen.getByRole('button', { name: "Refresh this bot's money" })).toBeTruthy();
  });

  it('says it is reading while the money is on its way', async () => {
    await renderCard(vi.fn().mockReturnValue(new Promise<DeploymentBudgetView>(() => undefined)));

    expect(screen.getByRole('status').textContent).toBe("Reading this bot's money…");
  });

  it.each([['running', RUNNING, true], ['stopped', STOPPED, false]])('has no detectable accessibility violations when %s', async (_name, view, running) => {
    await renderCard(vi.fn().mockResolvedValue(view), { running, holdsShares: true, openPnl: 12.5 });
    await screen.findByText(view.headline);

    await expectAxeClean();
  });
});
