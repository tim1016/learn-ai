import { TestBed } from '@angular/core/testing';
import { RouterTestingHarness } from '@angular/router/testing';
import { fireEvent, screen, waitFor, within } from '@testing-library/angular';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { configurationReview, fleetPage, goldenDeclaration, harnessEntry, proofRecord, qualification, registryEntry, runningBot, strategyInfo } from './testing/fixtures';
import { emptyWorld, provideSignalStrategyWorld, type SignalStrategyReads, type SignalStrategyWorld } from './testing/signal-strategy-world';

const EMA = 'ema_crossover_signal';

afterEach(() => {
  TestBed.resetTestingModule();
});

async function open(url: string, world: SignalStrategyWorld, reads: Partial<SignalStrategyReads> = {}) {
  TestBed.configureTestingModule({ providers: provideSignalStrategyWorld(world, reads) });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(url);
  return harness;
}

/** The section a heading opens, for asserting on what sits beneath that heading only. */
async function sectionOf(title: string): Promise<HTMLElement> {
  const heading = await screen.findByRole('heading', { name: title });
  const section = heading.closest('section');
  if (section === null) throw new Error(`${title} has no section around it`);
  return section;
}

describe('a strategy’s place', () => {
  it('fails in place for a key the registry does not list, with the way back, and opens no tab page', async () => {
    await open('/signal-strategies/no_such_strategy/overview', emptyWorld({ entries: [registryEntry()] }));

    expect(await screen.findByRole('heading', { level: 1, name: 'Unknown strategy' })).toBeTruthy();
    expect(screen.getByText('No registered strategy has the key "no_such_strategy".')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Back to Signal strategies' }).getAttribute('href')).toBe('/signal-strategies');
    expect(screen.queryByRole('heading', { name: 'Program proof' })).toBeNull();
  });

  it('says the key cannot be checked while the registry cannot be read, and reads it again on request', async () => {
    const entries = vi.fn();
    const world = emptyWorld({ entries: new Error('registry down') });
    await open(`/signal-strategies/${EMA}/overview`, world, { entries });

    expect(await screen.findByText('The strategy registry could not be read.')).toBeTruthy();
    expect(screen.queryByText(/No registered strategy has the key/)).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: 'Read it again' }));

    await waitFor(() => expect(entries).toHaveBeenCalledTimes(2));
    expect(await screen.findByText('The strategy registry could not be read.')).toBeTruthy();
  });

  it('links the tabs it has pages for, and shows the rest unavailable with the reason in view', async () => {
    await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()] }));

    const strip = await screen.findByRole('navigation', { name: 'Strategy sections' });
    for (const tab of ['Overview', 'Define', 'Explore']) {
      expect(within(strip).getByRole('link', { name: tab })).toBeTruthy();
    }
    expect(within(strip).getByRole('link', { name: 'Explore' }).getAttribute('href')).toBe(`/signal-strategies/${EMA}/explore`);
    for (const tab of ['Golden', 'Proof']) {
      expect(within(strip).queryByRole('link', { name: tab })).toBeNull();
      expect(within(strip).getByText(tab).getAttribute('aria-disabled')).toBe('true');
    }
    // The reason is in view beside the strip, not only read out from it.
    expect(screen.getByText('Golden', { selector: 'strong' }).closest('li')?.textContent).toMatch(/Golden Search and configuration review open here in an upcoming change/);
    expect(screen.queryByText('Explore', { selector: 'strong' })).toBeNull();
  });

  it('reads nothing again when the owner moves between tabs', async () => {
    const qualifications = vi.fn();
    const harness = await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()] }), { qualifications });
    await screen.findByRole('heading', { name: 'Qualified versions' });

    await harness.navigateByUrl(`/signal-strategies/${EMA}/define`);
    await screen.findByRole('heading', { name: 'Declared parameters' });
    await harness.navigateByUrl(`/signal-strategies/${EMA}/overview`);
    await screen.findByRole('heading', { name: 'Qualified versions' });

    expect(qualifications).toHaveBeenCalledTimes(1);
  });
});

describe('Overview', () => {
  it('shows a strategy with no proof record as having none, and no version or bot as known', async () => {
    await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()], proof: [proofRecord('some_other_strategy')] }));

    const proof = await sectionOf('Program proof');
    expect(await within(proof).findByText(/holds no record for this strategy/)).toBeTruthy();
    const versions = await sectionOf('Qualified versions');
    expect(await within(versions).findByText('No symbol has a qualified version of this strategy.')).toBeTruthy();
    expect(screen.queryByText(/Program proof: /)).toBeNull();
  });

  it('shows an operational harness as refused its sweep and Golden Search and capped below Live', async () => {
    await open('/signal-strategies/deployment_validation/overview', emptyWorld({ entries: [harnessEntry()], proof: [proofRecord('deployment_validation', 'current')] }));

    const allowed = await sectionOf('What the registry allows');
    expect(within(allowed).getByText('Sweep: not allowed')).toBeTruthy();
    expect(within(allowed).getByText('Golden Search: not allowed')).toBeTruthy();
    expect(within(allowed).getByText('Live: not allowed')).toBeTruthy();
    expect(within(allowed).getByText(/runs in Dry Run and Paper only; it stays outside Live/)).toBeTruthy();
    expect(screen.getByText('Experimental validation only — not a trading strategy')).toBeTruthy();
    // The reason Golden Search is refused is the reason no version exists.
    const versions = await sectionOf('Qualified versions');
    expect(await within(versions).findByText('This strategy is an operational harness, not a production candidate.')).toBeTruthy();
  });

  it('keeps ready, stale, revoked and unverifiable versions with the symbol each belongs to, never as a strategy-wide verdict', async () => {
    await open(
      `/signal-strategies/${EMA}/overview`,
      emptyWorld({
        entries: [registryEntry()],
        proof: [proofRecord(EMA, 'current')],
        qualifications: [
          qualification({ id: 'spy-ready', symbol: 'SPY', status: 'ready', is_default: true, created_at_ms: 3 }),
          qualification({ id: 'spy-stale', symbol: 'SPY', status: 'stale', status_explanation: 'The program changed since approval.', created_at_ms: 2 }),
          qualification({ id: 'qqq-revoked', symbol: 'QQQ', status: 'revoked', created_at_ms: 2 }),
          qualification({ id: 'qqq-unknown', symbol: 'QQQ', status: 'unverifiable', created_at_ms: 1 }),
        ],
      }),
    );

    const versions = await sectionOf('Qualified versions');
    const groups = await within(versions).findAllByRole('heading', { level: 3 });
    const [qqq, spy] = groups.map((heading) => heading.parentElement as HTMLElement);

    expect(within(qqq).getByText('QQQ')).toBeTruthy();
    expect(within(qqq).getByText('1 revoked · 1 unverifiable')).toBeTruthy();
    expect(within(qqq).queryByText('Ready')).toBeNull();
    expect(within(spy).getByText('SPY')).toBeTruthy();
    expect(within(spy).getByText('1 ready · 1 stale')).toBeTruthy();
    expect(within(spy).getByText('The program changed since approval.')).toBeTruthy();
    expect(within(spy).getByText('default for SPY')).toBeTruthy();
    expect(within(qqq).queryByText(/default for/)).toBeNull();
  });

  it('shows a registry corpus point and a reviewed configuration for a symbol, each scoped to what it covers', async () => {
    await open(
      `/signal-strategies/${EMA}/overview`,
      emptyWorld({ entries: [registryEntry()], reviews: [configurationReview({ symbol: 'QQQ', review_is_current: false })] }),
    );

    const corpus = await sectionOf('Registry corpus point');
    expect(within(corpus).getByText('fast_period = 5 · slow_period = 10')).toBeTruthy();
    const reviewed = await sectionOf('Reviewed configurations');
    const row = (await within(reviewed).findByText('QQQ')).closest('tr') as HTMLElement;
    expect(within(row).getByText(/review no longer applies/)).toBeTruthy();
    expect(within(row).getByText('fast_period = 6 · slow_period = 12')).toBeTruthy();
  });

  it('says a failed fleet read leaves the bots unknown, and still shows the evidence that was read', async () => {
    await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()], proof: [proofRecord(EMA, 'current')], fleet: new Error('fleet down') }));

    const bots = await sectionOf('Running bots');
    expect(await within(bots).findByText('Running bots could not be read.')).toBeTruthy();
    expect(within(bots).getByText(/it is unknown until the read succeeds/)).toBeTruthy();
    expect(within(bots).queryByText(/No bot of this strategy is running/)).toBeNull();
    expect(await within(await sectionOf('Program proof')).findByText(/4 of 4 stages complete/)).toBeTruthy();
  });

  it('never counts an account the fleet could not read as having none', async () => {
    const unreadable = { broker: 'alpaca', clerk_id: 'clrk_other', account_id: 'LIVE-1', strategy_instance_id: null, reason: 'The account could not be reached.', reason_code: 'clerk_unreachable' };
    await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()], fleet: fleetPage({ rows: [], gaps: [unreadable] }) }));

    const bots = await sectionOf('Running bots');

    expect(await within(bots).findByText(/Unknown: the fleet could not be read in full/)).toBeTruthy();
    expect(within(bots).queryByText(/No bot of this strategy is running/)).toBeNull();
    expect(within(bots).getByText(/The account could not be reached\./)).toBeTruthy();
  });

  it('lists only this strategy’s running bots, with account, mode and when the fleet was read, as a lower bound if incomplete', async () => {
    const unreadable = { broker: 'alpaca', clerk_id: 'clrk_other', account_id: 'LIVE-1', strategy_instance_id: null, reason: 'unreachable', reason_code: null };
    await open(
      `/signal-strategies/${EMA}/overview`,
      emptyWorld({
        entries: [registryEntry()],
        fleet: fleetPage({
          rows: [runningBot({ strategy_instance_id: 'mine' }), runningBot({ strategy_instance_id: 'theirs', strategy_key: 'sma_crossover' })],
          gaps: [unreadable],
        }),
      }),
    );

    const bots = await sectionOf('Running bots');

    expect(await within(bots).findByText(/At least 1 running/)).toBeTruthy();
    expect(within(bots).getByText('mine')).toBeTruthy();
    expect(within(bots).queryByText('theirs')).toBeNull();
    expect(within(bots).getByText(/PAPER/)).toBeTruthy();
    expect(within(bots).getByText(/as of/)).toBeTruthy();
  });

  it('says none are running only when the whole fleet was read', async () => {
    await open(`/signal-strategies/${EMA}/overview`, emptyWorld({ entries: [registryEntry()] }));

    expect(await within(await sectionOf('Running bots')).findByText(/No bot of this strategy is running/)).toBeTruthy();
  });
});

describe('Define', () => {
  const world = (overrides: Partial<SignalStrategyWorld> = {}) =>
    emptyWorld({
      entries: [registryEntry({ validated_settings: { fast_period: 6, slow_period: 10 } })],
      schemas: [strategyInfo()],
      capabilities: [goldenDeclaration()],
      qualifications: [qualification({ parameters: { fast_period: 8, slow_period: 20 } })],
      reviews: [configurationReview()],
      ...overrides,
    });

  it('keeps the registry default, the validated corpus value, the allowed range and the approved values apart', async () => {
    await open(`/signal-strategies/${EMA}/define`, world());

    const declared = await sectionOf('Declared parameters');
    const row = (await within(declared).findByText('fast_period')).closest('tr') as HTMLElement;
    const cells = within(row).getAllByRole('cell').map((cell) => cell.textContent?.trim());
    // Parameter, allowed range, registry default, validated corpus value, Golden Search range.
    expect(cells.slice(1)).toEqual(['≥ 2 and ≤ 200', '5', '6', '2 to 30, step 1']);
    expect(within(declared).getByText(/The fast EMA length must be shorter than the slow one/)).toBeTruthy();

    const approved = await sectionOf('Approved configuration values');
    expect(await within(approved).findByText('fast_period = 8 · slow_period = 20')).toBeTruthy();
    expect(within(approved).getByText('fast_period = 6 · slow_period = 12')).toBeTruthy();
    expect(within(declared).queryByText(/fast_period = 8/)).toBeNull();

    const exploratory = await sectionOf('Current exploratory values');
    expect(within(exploratory).getByText(/Nothing has been tried out for this strategy yet/)).toBeTruthy();
  });

  it('shows a missing description and rule statement as missing, and writes none', async () => {
    await open(
      `/signal-strategies/${EMA}/define`,
      world({ entries: [registryEntry({ description: '', rule_statement: null, program_version: null })] }),
    );

    expect(await screen.findByText('No description is recorded for this strategy in the registry.')).toBeTruthy();
    expect(screen.getByText(/no governed Signal Program, so no statement of its rules is recorded/)).toBeTruthy();
  });

  it('shows where Python declares nothing for a parameter, rather than a number', async () => {
    await open(`/signal-strategies/${EMA}/define`, world({ capabilities: [] }));

    const declared = await sectionOf('Declared parameters');
    const row = (await within(declared).findByText('gap')).closest('tr') as HTMLElement;

    // The validated corpus value and the Golden Search range; its default and allowed range are declared.
    expect(within(row).getAllByText('not declared')).toHaveLength(2);
    expect(within(declared).getByText(/No Golden Search declaration lists a constraint/)).toBeTruthy();
  });

  it('turns only the declared parameters to unknown when their read fails', async () => {
    await open(`/signal-strategies/${EMA}/define`, world({ schemas: new Error('catalog down') }));

    const declared = await sectionOf('Declared parameters');

    expect(await within(declared).findByText('Declared parameters could not be read.')).toBeTruthy();
    expect(within(await sectionOf('Description')).getByText(/Long-only intraday EMA signal generator/)).toBeTruthy();
  });

  it('opens the registered source in a drawer on request', async () => {
    await open(`/signal-strategies/${EMA}/define`, world());

    fireEvent.click(await screen.findByRole('button', { name: 'View source' }));

    expect(await screen.findByText('This strategy has no registered LEAN validation source.')).toBeTruthy();
  });

  it('offers Strategy Docs through “How this works” on Define, and not on Overview', async () => {
    const harness = await open(`/signal-strategies/${EMA}/define`, world());

    const link = await screen.findByRole('link', { name: /How this works/ });
    expect(link.getAttribute('href')).toBe('/strategy-docs');

    await harness.navigateByUrl(`/signal-strategies/${EMA}/overview`);
    await screen.findByRole('heading', { name: 'Program proof' });
    expect(screen.queryByRole('link', { name: /How this works/ })).toBeNull();
  });
});
