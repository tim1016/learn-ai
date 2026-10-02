import AxeBuilder from '@axe-core/playwright';
import { expect, test, type Locator, type Page } from '@playwright/test';

import type { DeploySubmissionBody } from '../../src/app/components/broker/v2-panel/lib/broker-v2-panel.service';
import type { BotClearRequest, PanelActionRequest } from '../../src/app/components/broker/v2-panel/lib/broker-v2-panel.types';
import type { CommandContext } from '../../src/app/fleet/resource-target';
import {
  BOT_NAME_NOTE,
  CLEAR_REFUSAL,
  DRY_RUN_BOT,
  DV_EXPERIMENTAL_NOTICE,
  EARLIER_BOT,
  HOLDING_ATTENTION,
  HOLDING_EXPLANATION,
  LIVE_ACCOUNT,
  LIVE_API,
  LIVE_BOT,
  LIVE_BUDGET,
  LIVE_CLEARED_BOT,
  LIVE_PHRASE,
  LIVE_WORKSPACE,
  NO_END_STOPPED,
  OwnerWalkWorld,
  PAPER_ACCOUNT,
  PAPER_API,
  PAPER_BUDGET,
  PAPER_HISTORY,
  PAPER_WORKSPACE,
  PLAN_NEXT_STEP,
  RECEIPT_EXPLANATION,
  RECEIPT_MESSAGE,
  RECONCILE_MESSAGE,
  RUNNING_EXPLANATION,
  SELL_MESSAGE,
  STOP_MESSAGE,
  WALKED_BOT,
  WALK_DEFAULT_END,
  WORLD_LABELS,
} from './support/owner-walk-world';

/**
 * The owner's walk through one account (PRD #2560 "Daily browser
 * walk-through", slice 7 #2567): Deploy → the bot's new slice on Home's money
 * bar → Stop → the stopped-but-holding row → Flatten → Finished → Clear.
 * After a Clear the owner finds the bot in History (#2614): the Finished
 * fold's link → the cleared bots → the bot's own page → Deploy again, for a
 * Paper bot and for a cleared Dry Run.
 *
 * Everything the page reads and sends is answered at the network edge by
 * `OwnerWalkWorld` — no coordinator, clerk or broker — so this walk can never
 * place an order. The world advances only when the page sends the command
 * that moves it (a Deploy, a Stop, the flatten sequence's sale, a Clear), and
 * the page's own polls carry each change onto the screen. Every dollar on
 * screen is asserted as the Python-authored string the world served.
 *
 * What this walk does not cover, and why:
 * - The bot buying its share. The world moves from "deployed" straight to
 *   "running, holding 1 SPY"; fills stream over the bot's SSE feed, which is
 *   answered empty here, so no fill marker or trade row is walked.
 * - A flatten outside regular hours. The prepared sale is priced
 *   `regular_session`, so the extended-hours limit ticket is not opened.
 * - The Wall view and the cohort flatten drawer: each has its own unit spec;
 *   this walk stays on the List.
 * - A Deploy again sent: the History walk stops at the pre-filled form.
 * - Home's attention dot on the top-bar pill: the directory's counts are
 *   served static, so the pill's dot is not asserted to move.
 * - A Live bot's life after its Deploy: the Live walk stops at the receipt.
 * - The bot chart's history, its indicator menu and the run timing are left
 *   unanswered on purpose (`UNANSWERED_ON_PURPOSE` in the world): the page
 *   degrades each in place and every control the walk uses stays usable. Any
 *   other unanswered `/api/` read fails the walk.
 */

/** The fleet envelope every command carries: the capability, the frozen
 * lane generation, and the account and bot it is aimed at. */
function envelopeOf(body: unknown): CommandContext {
  return (body as { command_context: CommandContext }).command_context;
}

function step(page: Page, name: 'What' | 'How' | 'Money' | 'Confirm'): Locator {
  return page.getByRole('region', { name });
}

/** The lane's attention read is its own 5 s poll, a step behind the command
 * that changed it: an attention line is awaited across one full poll. */
const ATTENTION_POLL = { timeout: 12_000 };

/** One of the account header's figures (Free to deploy, Cash, Equity, Today). */
function headerFigure(page: Page, term: string): Locator {
  return page
    .locator('header.account-workspace__header div')
    .filter({ has: page.getByText(term, { exact: true }) })
    .locator('dd');
}

/** The account header's "Deploy a bot" button (PRD #2560 D3), not Home's empty-state link. */
function headerDeploy(page: Page): Locator {
  return page.locator('header.account-workspace__header').getByRole('link', { name: 'Deploy a bot' });
}

/** Home in the account's tab strip (Home · Activity · Settings). */
function homeTab(page: Page): Locator {
  return page.getByRole('navigation', { name: 'Account sections' }).getByRole('link', { name: 'Home' });
}

function botsList(page: Page): Locator {
  return page.getByRole('list', { name: 'Bots', exact: true });
}

/** One flatten step as the warning lists it: its state, then the backend's
 * own words for how it went, when it said any. */
function flattenStep(state: string, message?: string): RegExp {
  const escape = (text: string) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  return new RegExp(`^${escape(state)}${message === undefined ? '' : `\\s*${escape(message)}`}$`);
}

/** One bot's row on Home's List: the item holding the link to its page. */
function botRow(page: Page, sid: string): Locator {
  return botsList(page).getByRole('listitem').filter({ has: page.getByRole('link', { name: sid, exact: true }) });
}

/** Home's Finished list: a fold, closed until the owner opens it. */
function finishedFold(page: Page): Locator {
  return page.locator('details').filter({ has: page.getByText('Finished', { exact: true }) });
}

/** The History tab: every bot across every account (#2574). */
function historyTab(page: Page): Locator {
  return page.getByRole('region', { name: 'History' });
}

/** The bots History lists, in its order, by the one link that opens each. */
function historyBots(page: Page): Locator {
  return historyTab(page).getByRole('table').getByRole('link');
}

/** One bot's line in History. */
function historyRow(page: Page, sid: string): Locator {
  return historyTab(page).getByRole('row').filter({ has: page.getByRole('link', { name: sid, exact: true }) });
}

/** The bot page's banner, where a cleared bot says so (#2794). */
function botBanner(page: Page): Locator {
  return page.locator('app-bot-page-header');
}

/** What the bot page says under the tabs: cleared, experimental, needs attention. */
function botNotices(page: Page): Locator {
  return page.locator('app-bot-page-notices');
}

/** The bot page's toolbar, where a stopped bot offers Deploy again (#2794). */
function botToolbar(page: Page): Locator {
  return page.getByRole('toolbar', { name: 'Actions for this bot' });
}

/** The page as the app shell serves it passes AXE, landmarks and all. */
async function expectNoAxeViolations(page: Page): Promise<void> {
  const axe = await new AxeBuilder({ page }).analyze();
  expect(axe.violations.map(({ id, nodes }) => `${id}: ${nodes.map((node) => node.target.join(' ')).join(' | ')}`)).toEqual([]);
}

/** `bot-page-header.component.html`'s note on a cleared bot's page. */
const CLEARED_NOTE = 'Cleared: this bot will not run again, and its records here are read-only.';

test.describe('The owner walks one account (PRD #2560)', () => {
  // One test is the whole walk: a dozen screens and every command in turn.
  test.setTimeout(60_000);

  test('Paper: deploys a bot, sees its slice, stops it, flattens what it holds, and clears it', async ({ page }) => {
    const world = new OwnerWalkWorld();
    await world.install(page);

    // ── Home before: all $25,000.00 free, one bot finished days ago. ──────
    await page.goto(PAPER_WORKSPACE);
    const home = page.getByRole('region', { name: 'Home' });
    const freeToDeploy = headerFigure(page, 'Free to deploy');
    await expect(freeToDeploy).toHaveText('$25,000.00');
    const accountBar = home.getByRole('list', { name: 'Where the money is' });
    await expect(accountBar).toContainText('free to deploy $25,000.00');
    await expect(home.getByText('No bots are running.')).toBeVisible();

    // ── Deploy: the header's button opens the four steps. ──────────────────
    await headerDeploy(page).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/deploy`);
    await expect(page.getByRole('heading', { level: 2 })).toHaveText(['What', 'How', 'Money', 'Confirm']);

    // The steps stand side by side, every one open. What and How are
    // complete from the server's offer, so each is headed Ready.
    await expect(step(page, 'What').getByText('Ready', { exact: true })).toBeVisible();
    await expect(step(page, 'What')).toContainText('Allowed on this Paper account');
    await expect(step(page, 'What').getByRole('button', { name: /^(Edit|Done with) step/ })).toHaveCount(0);
    await expect(step(page, 'How').getByText('Ready', { exact: true })).toBeVisible();
    await expect(step(page, 'How').getByRole('radio', { name: /Paper/ })).toBeChecked();
    await expect(step(page, 'How').getByRole('spinbutton', { name: 'Exit allowance (bps)' })).toHaveValue('20');
    // Deployment Validation says it is no trading strategy; its end is the
    // account's default, in the backend's words (#2607).
    await expect(step(page, 'What').getByRole('note')).toContainText(DV_EXPERIMENTAL_NOTICE);
    const deployEnd = page.getByRole('region', { name: 'End · changeable later' });
    await expect(deployEnd).toContainText(WALK_DEFAULT_END.headline);
    await expect(deployEnd).toContainText('15:59:00 ET');

    // Money: the account's bar as it stands, then a NEW slice carved from
    // free to deploy once the server has previewed the typed amount.
    const money = step(page, 'Money');
    await expect(money.getByRole('list', { name: 'Where the money is now' })).toContainText('free to deploy $25,000.00');
    await expect(money.getByText('new bot')).toHaveCount(0);
    await money.getByLabel('Dollar budget (USD)').fill(PAPER_BUDGET);
    const after = money.getByRole('list', { name: 'Account money after this Deploy' });
    await expect(after).toContainText('new bot $1,000.00');
    await expect(after).toContainText('free to deploy $24,000.00');
    await expect(money.getByRole('status')).toContainText('$1,000.00 would be set aside for this bot.');

    // Confirm: the review names what the backend will name the bot; Paper
    // asks for no typed phrase; the button names the world and the amount.
    const confirm = step(page, 'Confirm');
    const review = confirm.getByLabel('Deploy review');
    await expect(review).toContainText(BOT_NAME_NOTE);
    await expect(review).toContainText(`${PAPER_ACCOUNT} · PAPER · practice money`);
    await expect(confirm.getByLabel(/To deploy with real money, type/)).toHaveCount(0);
    const deploy = confirm.getByRole('button', { name: 'Deploy paper bot · set aside $1,000.00' });
    await expect(deploy).toBeEnabled();
    await deploy.click();

    // The receipt names the bot the backend authored, and takes the keyboard.
    const receipt = page.getByRole('status').filter({ has: page.getByRole('heading', { name: RECEIPT_MESSAGE }) });
    await expect(receipt).toBeVisible();
    await expect(receipt).toBeFocused();
    await expect(receipt).toContainText(RECEIPT_EXPLANATION);
    await expect(receipt).toContainText(WALKED_BOT);
    await expect(receipt.getByRole('link', { name: `Open ${WALKED_BOT}` })).toBeVisible();
    const [sent] = world.commandsTo(`/accounts/${PAPER_ACCOUNT}/bots`);
    expect(sent.path).toBe(`${PAPER_API}/bots`);
    const submission = sent.body as DeploySubmissionBody;
    // The browser sends an opaque submission key and never a bot name (#2551).
    expect(submission).not.toHaveProperty('strategy_instance_id');
    expect(submission.submission_key).toMatch(/^[A-Za-z0-9_-]{8,64}$/);
    expect(submission.execution_mode).toBe('paper');
    expect(submission.budget).toEqual({
      amount_usd: PAPER_BUDGET,
      risk_revision: 1,
      review_token: `review-real_paper-${PAPER_BUDGET}`,
      live_confirmation: null,
    });
    // The end on screen always rides the Deploy, its action included.
    expect(submission.end).toEqual({ end_at_ms: WALK_DEFAULT_END.end_at_ms, end_action: 'SELL' });

    // ── Home after: the bot's own slice, carved from free to deploy. ───────
    await homeTab(page).click();
    await expect(page).toHaveURL(PAPER_WORKSPACE);
    const row = botRow(page, WALKED_BOT);
    await expect(row).toContainText(RUNNING_EXPLANATION);
    await expect(accountBar).toContainText(
      `${WALKED_BOT} $1,000.00 in shares $500.00 · in entry orders $0.00 · free $500.00`,
    );
    await expect(accountBar).toContainText('free to deploy $24,000.00');
    await expect(freeToDeploy).toHaveText('$24,000.00');
    await expect(row).toContainText('balance $1,000.00 · free $500.00');
    await expect(home.getByText('1 running · 0 stopped, still holding')).toBeVisible();

    // ── Stop, asked in the Clerk's own words, as the bot page asks it. ───────
    await row.getByRole('button', { name: `Stop ${WALKED_BOT}` }).click();
    const askStop = page.getByRole('dialog', { name: `Stop ${WALKED_BOT}?` });
    await expect(askStop).toContainText('The bot stops making new decisions. A sale already sent can still go through. Cash it isn\'t using goes back to the account. Its scheduled end is cancelled: nothing is sold at the end time.');
    await expect(askStop.getByRole('button', { name: 'Cancel' })).toBeFocused();
    await askStop.getByRole('button', { name: 'Stop bot decisions' }).click();
    const stopOutcome = home.getByRole('status').filter({ hasText: STOP_MESSAGE });
    await expect(stopOutcome).toBeFocused();
    const [stop] = world.commandsTo(`/bots/${WALKED_BOT}/actions/quiesce`);
    expect((stop.body as PanelActionRequest).action_id).toBe('stop_bot_decisions');
    expect(envelopeOf(stop.body).target).toEqual({ account_id: PAPER_ACCOUNT, entity_id: WALKED_BOT });

    // ── The stopped bot still holds 1 SPY: it stays on the list, with its fix. ─
    const holdingRow = botRow(page, WALKED_BOT);
    await expect(holdingRow).toContainText(HOLDING_EXPLANATION);
    await expect(holdingRow).toContainText('held $500.00 · released $500.00');
    await expect(accountBar).toContainText(`held by stopped bot ${WALKED_BOT} $500.00 released $500.00`);
    await expect(accountBar).toContainText('free to deploy $24,500.00');
    await expect(freeToDeploy).toHaveText('$24,500.00');
    await expect(home.getByText('0 running · 1 stopped, still holding')).toBeVisible();
    const attention = home.getByRole('list', { name: 'Needs attention' });
    await expect(attention).toContainText(HOLDING_ATTENTION, ATTENTION_POLL);
    await expect(attention.getByRole('link', { name: 'Flatten…' })).toHaveAttribute(
      'href', `${PAPER_WORKSPACE}/bots/${WALKED_BOT}`,
    );

    // ── Flatten…: the bot page's warning and its one confirmed sequence. ──────
    await holdingRow.getByRole('link', { name: `Flatten ${WALKED_BOT}…` }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/bots/${WALKED_BOT}`);
    // The Stop cancelled the planned end sale: nothing is left to end, or to change.
    const stoppedEnd = page.getByRole('region', { name: 'End', exact: true });
    await expect(stoppedEnd).toContainText(NO_END_STOPPED.headline);
    await expect(stoppedEnd.getByRole('button', { name: 'Change end' })).toHaveCount(0);
    const warning = page.getByRole('region', { name: 'No bot is managing 1 SPY' });
    await expect(warning).toContainText('Deploy again starts a new bot with its own budget. It never takes over these shares.');
    await warning.getByRole('button', { name: 'Flatten…' }).click();
    const sell = warning.getByRole('button', { name: 'Sell 1 SPY' });
    await expect(sell).toBeFocused();
    await expect(warning.getByRole('group', { name: 'Sell 1 SPY?' })).toContainText('The position is checked with Alpaca first.');
    // The sale is held in flight: the steps say where the sequence is.
    const releaseSale = world.hold('execute_safe_flatten');
    await sell.click();
    const progress = warning.getByRole('list', { name: 'Flatten progress' });
    await expect(progress.getByRole('listitem')).toHaveText([
      flattenStep('Check the position with Alpaca: Done', RECONCILE_MESSAGE),
      flattenStep('Prepare the sale: Done', PLAN_NEXT_STEP),
      flattenStep('Sell: In progress'),
    ]);
    releaseSale();

    // Sold: the outcome takes the keyboard, and with nothing left unmanaged
    // the warning is gone.
    const sold = page.getByRole('status', { name: 'Action outcome' });
    await expect(sold).toContainText(SELL_MESSAGE);
    await expect(sold).toBeFocused();
    await expect(warning).toHaveCount(0);
    // Every command was aimed at this bot, in the sequence's order.
    const botCommands = world.sent.filter((command) => command.path.includes(`/bots/${WALKED_BOT}/`));
    expect(botCommands.map((command) => `${command.path.split('/').slice(-2).join('/')} ${(command.body as { action_id: string }).action_id}`)).toEqual([
      'actions/quiesce stop_bot_decisions',
      'actions/quiesce reconcile_now',
      'recovery-actions/check prepare_safe_flatten',
      'actions/quiesce execute_safe_flatten',
    ]);
    for (const command of botCommands.filter((sentCommand) => sentCommand.path.endsWith('/actions/quiesce'))) {
      expect(envelopeOf(command.body).target).toEqual({ account_id: PAPER_ACCOUNT, entity_id: WALKED_BOT });
    }

    // ── Home: flat and released, the bot moved into the folded Finished list. ─
    await homeTab(page).click();
    await expect(botsList(page)).toHaveCount(0);
    await expect(accountBar).toHaveText('free to deploy $25,000.99');
    await expect(freeToDeploy).toHaveText('$25,000.99');
    await expect(home.getByRole('list', { name: 'Needs attention' })).toHaveCount(0, ATTENTION_POLL);
    const finished = finishedFold(page);
    await expect(finished).not.toHaveAttribute('open');
    await expect(finished.locator('summary')).toHaveText('Finished (2) · flat, all money returned');
    await finished.locator('summary').click();
    const walkedFinished = finished.getByRole('row').filter({ hasText: WALKED_BOT });
    await expect(walkedFinished).toContainText('$0.99');
    await expect(walkedFinished.getByRole('link', { name: /Deploy again/ })).toBeVisible();

    // ── Clear finished: tick both, confirm, read each bot's outcome. ──────────
    await finished.getByRole('checkbox', { name: `Select ${WALKED_BOT}` }).check();
    await finished.getByRole('checkbox', { name: `Select ${EARLIER_BOT}` }).check();
    await expect(finished.getByRole('checkbox', { name: 'Select all finished bots' })).toBeChecked();
    await expect(finished.getByRole('status')).toHaveText('2 selected.');
    await finished.getByRole('button', { name: 'Clear selected (2)' }).click();
    const dialog = page.getByRole('dialog', { name: 'Clear 2 finished bots from Home?' });
    await expect(dialog).toContainText('Their history stays in Activity: runs, fills, fees and results.');
    await dialog.getByRole('button', { name: 'Clear 2' }).click();

    const outcome = finished.getByRole('region', { name: 'Clear outcome' });
    const summary = outcome.getByRole('alert');
    await expect(summary).toHaveText('Cleared 1 of 2 bots. 1 not cleared.');
    await expect(summary).toBeFocused();
    await expect(outcome.getByRole('listitem').filter({ hasText: WALKED_BOT })).toContainText('Cleared');
    const refused = outcome.getByRole('listitem').filter({ hasText: EARLIER_BOT });
    await expect(refused).toContainText('Not cleared');
    await expect(refused).toContainText(CLEAR_REFUSAL.message);
    await expect(refused).toContainText(CLEAR_REFUSAL.why);
    const [clear] = world.commandsTo('/bots/clear');
    const clearBody = clear.body as BotClearRequest;
    expect([...clearBody.strategy_instance_ids].sort()).toEqual([EARLIER_BOT, WALKED_BOT].sort());
    expect(clearBody.idempotency_key).toMatch(/^[0-9a-f-]{36}$/);
    expect(envelopeOf(clear.body).idempotency_key).toBe(clearBody.idempotency_key);

    // The cleared bot leaves Finished; the refused one stays.
    await expect(finished.getByRole('row').filter({ hasText: WALKED_BOT })).toHaveCount(0);
    await expect(finished.getByRole('row').filter({ hasText: EARLIER_BOT })).toHaveCount(1);
    await expect(finished.locator('summary')).toHaveText('Finished (1) · flat, all money returned');

    // Nothing the Paper walk did reached the Live account (FR-096).
    expect(world.sent.filter((command) => !command.path.startsWith(PAPER_API))).toEqual([]);
    expect(world.unexpected()).toEqual([]);
  });

  test('History: a cleared bot and a cleared Dry Run are found there, open, and deploy again (#2614)', async ({ page }) => {
    const world = new OwnerWalkWorld();
    world.phase = 'finished';
    await world.install(page);

    // ── Home: clear the finished bot from the Finished fold. ──────────────────
    await page.goto(PAPER_WORKSPACE);
    const finished = finishedFold(page);
    await expect(finished).toBeVisible();
    await expectNoAxeViolations(page);
    await finished.locator('summary').click();
    await finished.getByRole('checkbox', { name: `Select ${WALKED_BOT}` }).check();
    await finished.getByRole('button', { name: 'Clear selected (1)' }).click();
    await page.getByRole('dialog', { name: 'Clear 1 finished bot from Home?' }).getByRole('button', { name: 'Clear 1' }).click();
    await expect(finished.getByRole('region', { name: 'Clear outcome' })).toContainText('Cleared 1 of 1 bot.');
    await expect(finished.getByRole('row').filter({ hasText: WALKED_BOT })).toHaveCount(0);

    // ── The fold's link: History, on every account's cleared bots. ─────────────
    await finished.getByRole('link', { name: 'see cleared bots in History' }).click();
    await expect(page).toHaveURL(`${PAPER_HISTORY}?status=cleared`);
    const history = historyTab(page);
    await expect(history.getByRole('combobox', { name: 'Status', exact: true })).toHaveValue('cleared');
    await expect(history.getByRole('combobox', { name: 'Account', exact: true })).toHaveValue('');
    // The bot just cleared, a Dry Run cleared on this account and a bot
    // cleared on Live, newest first; the finished bot Home still shows is not.
    await expect(historyBots(page)).toHaveText([WALKED_BOT, DRY_RUN_BOT, LIVE_CLEARED_BOT]);
    await expect(history).toContainText('Page 1 of 1 · 3 bots');
    await expect(historyRow(page, WALKED_BOT)).toContainText('Cleared');
    // Each row wears the world its run was in, not its lane's mode (#2615).
    await expect(historyRow(page, DRY_RUN_BOT).locator('app-alpaca-lane-mode-chip')).toHaveText(WORLD_LABELS.dry_run);

    // The page passes AXE as the app shell serves it, landmarks and all.
    await expectNoAxeViolations(page);

    // ── The cleared bot's page, from its History row: read-only, Deploy again. ─
    await history.getByRole('link', { name: WALKED_BOT, exact: true }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/bots/${WALKED_BOT}`);
    await expect(botNotices(page)).toContainText(CLEARED_NOTE);
    await expect(botNotices(page).getByRole('link', { name: 'Every cleared bot is in History' }))
      .toHaveAttribute('href', `${PAPER_HISTORY}?status=cleared`);
    await botToolbar(page).getByRole('link', { name: 'Deploy again' }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/deploy?from=${WALKED_BOT}`);
    await expect(page.getByRole('region', { name: 'Deploy again' })).toContainText(`Prefilled from ${WALKED_BOT}.`);

    // ── History's own tab lists every bot; its World filter finds the Dry Run. ─
    await page.getByRole('navigation', { name: 'Account sections' }).getByRole('link', { name: 'History' }).click();
    await expect(page).toHaveURL(PAPER_HISTORY);
    await expect(historyBots(page)).toHaveText([WALKED_BOT, DRY_RUN_BOT, EARLIER_BOT, LIVE_CLEARED_BOT]);
    await history.getByRole('combobox', { name: 'World', exact: true }).selectOption({ label: 'Dry Run' });
    await expect(page).toHaveURL(`${PAPER_HISTORY}?world=dry_run`);
    await expect(historyBots(page)).toHaveText([DRY_RUN_BOT]);
    await expect(historyRow(page, DRY_RUN_BOT)).toContainText('Cleared');

    // ── The cleared Dry Run's page: simulated cash, read-only, Deploy again. ───
    await history.getByRole('link', { name: DRY_RUN_BOT, exact: true }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/bots/${DRY_RUN_BOT}`);
    await expect(botBanner(page)).toContainText(WORLD_LABELS.dry_run);
    await expect(botNotices(page)).toContainText(CLEARED_NOTE);
    await botToolbar(page).getByRole('link', { name: 'Deploy again' }).click();
    await expect(page).toHaveURL(`${PAPER_WORKSPACE}/deploy?from=${DRY_RUN_BOT}`);
    await expect(page.getByRole('region', { name: 'Deploy again' })).toContainText(`Prefilled from ${DRY_RUN_BOT}.`);

    // Deploy again only pre-fills: the one clear is all this walk sent.
    expect(world.commandsTo(`${PAPER_API}/bots`)).toEqual([]);
    expect(world.commandsTo('/bots/clear')).toHaveLength(1);
    expect(world.unexpected()).toEqual([]);
  });

  test('Deploy fits one screen: every step, every field and the button in view, nothing to scroll', async ({ page }) => {
    const world = new OwnerWalkWorld();
    await world.install(page);
    await page.setViewportSize({ width: 1440, height: 900 });

    await page.goto(`${PAPER_WORKSPACE}/deploy`);
    await step(page, 'Money').getByLabel('Dollar budget (USD)').fill(PAPER_BUDGET);
    const deploy = step(page, 'Confirm').getByRole('button', { name: 'Deploy paper bot · set aside $1,000.00' });
    await expect(deploy).toBeEnabled();

    await expect(deploy).toBeInViewport({ ratio: 1 });
    // The end and Deployment Validation's note are on the one screen too (#2607).
    await expect(page.getByRole('region', { name: 'End · changeable later' })).toBeInViewport({ ratio: 1 });
    await expect(step(page, 'What').getByRole('note')).toBeInViewport({ ratio: 1 });
    const fit = await page.evaluate(() => ({
      height: document.documentElement.scrollHeight,
      viewport: window.innerHeight,
    }));
    expect(fit.height).toBeLessThanOrEqual(fit.viewport);

    // Every field wears its own fill: the exit terms and the budget were once
    // drawn with no box at all, a label over nothing.
    const how = step(page, 'How');
    const column = await how.evaluate((el) => getComputedStyle(el).backgroundColor);
    for (const name of ['Exit allowance (bps)', 'Band multiple', 'Spread cap (bps)']) {
      const fill = await how.getByRole('spinbutton', { name }).evaluate((el) => getComputedStyle(el).backgroundColor);
      expect(fill, name).not.toBe('rgba(0, 0, 0, 0)');
      expect(fill, name).not.toBe(column);
    }
    const budgetFill = await step(page, 'Money').getByLabel('Dollar budget (USD)')
      .evaluate((el) => getComputedStyle(el).backgroundColor);
    expect(budgetFill).not.toBe('rgba(0, 0, 0, 0)');
    expect(world.unexpected()).toEqual([]);
  });

  test('Live: never preselects Live, and deploys only on the exact typed phrase', async ({ page }) => {
    const world = new OwnerWalkWorld();
    await world.install(page);

    await page.goto(LIVE_WORKSPACE);
    await expect(page.locator('header.account-workspace__header')).toContainText('LIVE · real money');
    await expect(headerFigure(page, 'Free to deploy')).toHaveText('$5,000.00');
    await headerDeploy(page).click();
    await expect(page).toHaveURL(`${LIVE_WORKSPACE}/deploy`);

    // Where it trades is the owner's choice: neither world is picked for them.
    const how = step(page, 'How');
    await expect(how.getByRole('radio', { name: /Live/ })).not.toBeChecked();
    await expect(how.getByRole('radio', { name: /Dry Run/ })).not.toBeChecked();
    const confirm = step(page, 'Confirm');
    await expect(confirm.getByRole('button', { name: 'Deploy bot', exact: true })).toBeDisabled();
    await expect(confirm).toContainText('Choose where this bot trades in How.');

    await how.getByRole('radio', { name: /Live/ }).check();
    const money = step(page, 'Money');
    await money.getByLabel('Dollar budget (USD)').fill(LIVE_BUDGET);
    await expect(money.getByRole('list', { name: 'Account money after this Deploy' })).toContainText('new bot $800.00');

    // The phrase is the backend's; the button stays shut until it is typed exactly.
    const deploy = confirm.getByRole('button', { name: 'Deploy live bot · set aside $800.00' });
    const phrase = confirm.getByLabel(/To deploy with real money, type/);
    await expect(confirm.getByText(LIVE_PHRASE, { exact: true })).toBeVisible();
    await expect(deploy).toBeDisabled();
    await phrase.fill(LIVE_PHRASE.toLowerCase());
    await expect(deploy).toBeDisabled();
    await expect(confirm).toContainText('Type the phrase above exactly to confirm this real-money Deploy.');
    await phrase.fill(`DEPLOY ${LIVE_ACCOUNT} $900.00`);
    await expect(deploy).toBeDisabled();
    // Nothing was sent on a wrong phrase.
    expect(world.commandsTo(`${LIVE_API}/bots`)).toEqual([]);
    await phrase.fill(LIVE_PHRASE);
    await expect(deploy).toBeEnabled();
    await expect(confirm.getByLabel('Deploy review')).toContainText(`${LIVE_ACCOUNT} · LIVE · real money`);

    // Sent against the mock only: the consent rides the request verbatim.
    await deploy.click();
    const receipt = page.getByRole('status').filter({ has: page.getByRole('heading', { name: `${LIVE_BOT} is deployed` }) });
    await expect(receipt).toBeFocused();
    await expect(receipt).toContainText(`$${LIVE_BUDGET} is set aside for it.`);
    const [sent] = world.commandsTo(`${LIVE_API}/bots`);
    const submission = sent.body as DeploySubmissionBody;
    expect(submission.execution_mode).toBe('live');
    expect(submission.budget).toEqual({
      amount_usd: LIVE_BUDGET,
      risk_revision: 1,
      review_token: `review-real_live-${LIVE_BUDGET}`,
      live_confirmation: LIVE_PHRASE,
    });

    // Nothing the Live walk did reached the Paper account (FR-096).
    expect(world.sent.filter((command) => !command.path.startsWith(LIVE_API))).toEqual([]);
    expect(world.unexpected()).toEqual([]);
  });

  test('Bot page: a running bot’s end is the owner’s to change, and the next read shows it (#2607)', async ({ page }) => {
    const world = new OwnerWalkWorld();
    world.phase = 'running';
    await world.install(page);

    await page.goto(`${PAPER_WORKSPACE}/bots/${WALKED_BOT}`);
    await expect(botNotices(page).getByRole('note')).toContainText(DV_EXPERIMENTAL_NOTICE);
    const end = page.getByRole('region', { name: 'End', exact: true });
    await expect(end).toContainText(WALK_DEFAULT_END.headline);
    await expect(end).toContainText(WALK_DEFAULT_END.explanation);

    // Change end sits on the toolbar (#2794); the End card shows the result.
    await botToolbar(page).getByRole('button', { name: 'Change end' }).click();
    const editor = page.getByRole('dialog', { name: 'Change this bot’s end' });
    await expect(editor.getByLabel('End date')).toBeFocused();
    // The time is typed in the owner's own zone, with the market's beside it.
    await expect(editor.getByText(/^Market time/)).toHaveText('Market time 15:59:00 ET');
    await editor.getByRole('radio', { name: 'Keep its shares' }).check();
    await editor.getByRole('button', { name: 'Save end' }).click();

    await expect(editor).toBeHidden();
    // `bot_end.bot_end_view`'s words for the kept end, read back from the panel.
    await expect(end).toContainText('Ends Mon Sep 21, 15:59 ET · keeps its shares');
    await expect(end).toContainText('At Mon Sep 21, 15:59 ET the Clerk stops the bot and cancels its working orders. It keeps its shares.');
    const [put] = world.commandsTo(`/bots/${WALKED_BOT}/end`);
    expect(put.method).toBe('PUT');
    expect(put.body).toMatchObject({ end_at_ms: WALK_DEFAULT_END.end_at_ms, end_action: 'KEEP' });
    expect(envelopeOf(put.body).target).toEqual({ account_id: PAPER_ACCOUNT, entity_id: WALKED_BOT });
    expect(world.unexpected()).toEqual([]);
  });
});
