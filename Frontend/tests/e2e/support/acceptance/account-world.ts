import type { AggregateAttentionResponse } from '../../../../src/app/services/lane-attention.service';
import {
  DIRECTORY,
  LIVE_ACCOUNT,
  LIVE_API,
  LIVE_CLERK,
  LIVE_MONEY,
  NOW_MS,
  PAPER_ACCOUNT,
  PAPER_API,
  PAPER_CLERK,
  PAPER_MONEY_BEFORE,
  admission,
  brokerAccount,
  budgetPreview,
  clerkStatus,
  endPreview,
  DEPLOY_LANES,
  verdict,
} from '../owner-walk-world';
import type { BotEndPreviewRequest, DeployBotBody } from '../../../../src/app/components/broker/v2-panel/lib/broker-v2-panel.service';
import type { Handler } from './mock-backend';
import { ok } from './mock-backend';

/**
 * The accounts side of the acceptance walks: two ready Alpaca lanes, Paper and
 * Live, with nothing running on either, answered at the network edge.
 *
 * Walks (b) and (c) end here: the account list, an account's Deploy page, and
 * the reads Deploy makes. Nothing in this world accepts a command. Deploy's
 * own reads (the budget preview, the admission check, the end preview) are
 * POSTs that only compute an answer from their body, so they are answered; the
 * Deploy submission itself, `POST .../bots`, is not, and a walk that sent it
 * would be refused with a 503 and fail on its ledger.
 */

export { LIVE_ACCOUNT, LIVE_CLERK, PAPER_ACCOUNT, PAPER_CLERK };

export const PAPER_WORKSPACE_PATH = `/brokers/alpaca/clerks/${PAPER_CLERK}/accounts/${PAPER_ACCOUNT}`;
export const LIVE_WORKSPACE_PATH = `/brokers/alpaca/clerks/${LIVE_CLERK}/accounts/${LIVE_ACCOUNT}`;

const lanePath = (clerk: string): string => `/api/brokers/alpaca/clerks/${clerk}`;

const NO_ATTENTION: AggregateAttentionResponse = {
  observed_at_ms: NOW_MS,
  lanes: [
    { broker: 'alpaca', clerk_id: PAPER_CLERK, ok: true, value: { account_id: PAPER_ACCOUNT, items: [] } },
    { broker: 'alpaca', clerk_id: LIVE_CLERK, ok: true, value: { account_id: LIVE_ACCOUNT, items: [] } },
  ],
};

/** The fleet, each lane's header reads and its money, with no bot on either account. */
export const accountHandlers: Handler = ({ method, path }) => {
  if (method !== 'GET') return null;
  if (path === '/api/broker-clerks') return ok(DIRECTORY);
  if (path === '/api/broker-clerks/aggregate/attention') return ok(NO_ATTENTION);
  if (path === `${lanePath(PAPER_CLERK)}/live-verdict`) return ok(verdict('paper', PAPER_ACCOUNT));
  if (path === `${lanePath(LIVE_CLERK)}/live-verdict`) return ok(verdict('live', LIVE_ACCOUNT));
  if (path === `${lanePath(PAPER_CLERK)}/account`) return ok(brokerAccount(PAPER_ACCOUNT, 'paper', 25_000));
  if (path === `${lanePath(LIVE_CLERK)}/account`) return ok(brokerAccount(LIVE_ACCOUNT, 'live', 5_000));
  if (path === `${lanePath(PAPER_CLERK)}/clerk/status`) return ok(clerkStatus(PAPER_ACCOUNT, 'real_paper'));
  if (path === `${lanePath(LIVE_CLERK)}/clerk/status`) return ok(clerkStatus(LIVE_ACCOUNT, 'real_live'));
  if (path === `${PAPER_API}/money`) return ok(PAPER_MONEY_BEFORE);
  if (path === `${LIVE_API}/money`) return ok(LIVE_MONEY);
  if (path === `${PAPER_API}/bots/catalog` || path === `${LIVE_API}/bots/catalog`) return ok([]);
  if (path === '/api/jobs') return ok([]);
  return null;
};

/**
 * Deploy's reads on either account. `GET .../bots/deploy` is the page's view;
 * the three POSTs only compute (the budget a typed amount would set aside,
 * whether the run would be admitted, and when the default end falls).
 */
export const deployReadHandlers: Handler = ({ method, path, body }) => {
  for (const [scope, lane] of [[PAPER_API, DEPLOY_LANES.paper], [LIVE_API, DEPLOY_LANES.live]] as const) {
    if (method === 'GET' && path === `${scope}/bots/deploy`) return ok(lane.view);
    if (method === 'POST' && path === `${scope}/bots/budget-preview`) {
      const preview = budgetPreview(lane, body as DeployBotBody);
      return preview === null ? null : ok(preview);
    }
    if (method === 'POST' && path === `${scope}/bots/admission`) return ok(admission(lane));
    if (method === 'POST' && path === `${scope}/bots/end-preview`) return ok(endPreview(body as BotEndPreviewRequest));
  }
  return null;
};
