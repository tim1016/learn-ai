import type { AccountMoneyView } from '../components/broker/v2-panel/lib/broker-v2-panel.service';

/** One ready account-money read, exactly as the backend authors it (every
 * dollar string and width below was produced by
 * `app/broker/alpaca/clerk/account_money.py` for a $100,000 Paper account
 * with one running bot, one stopped bot still holding 1 SPY, and a $0.01
 * fee Alpaca has not taken yet). */
export function fakeAccountMoney(overrides: Partial<AccountMoneyView> = {}): AccountMoneyView {
  return {
    state: 'ready',
    detail: 'Cash plus shares at the price paid.',
    world: 'real_paper',
    account_id: 'PA-TEST',
    observed_at_ms: 1_700_000_000_000,
    total_usd: '100000.00',
    cash_usd: '98564.86',
    free_to_deploy_usd: '98329.57',
    in_bots_usd: '999.99',
    held_by_stopped_usd: '670.43',
    outside_bots_usd: '0.00',
    account_charges_usd: '0.01',
    open_pnl_usd: '12.40',
    equity_usd: '100012.40',
    today_pnl_usd: '-3.20',
    segments: [
      {
        kind: 'bot',
        strategy_instance_id: 'spy-ema-20260929-0931',
        label: 'spy-ema-20260929-0931',
        amount_usd: '999.99',
        share_bps: 100,
        parts: {
          in_shares_usd: '764.71', in_shares_bps: 7647,
          pending_usd: '0.00', pending_bps: 0,
          free_usd: '235.28', free_bps: 2353,
        },
        shortfall_usd: '0.00',
      },
      {
        kind: 'stopped',
        strategy_instance_id: 'spy-ema-20260925-1402',
        label: 'held by stopped bot spy-ema-20260925-1402',
        amount_usd: '670.43',
        share_bps: 67,
        released_usd: '0.00',
        still_claimed_usd: '0.00',
      },
      { kind: 'charges', label: 'account charges', amount_usd: '0.01', share_bps: 1 },
      { kind: 'free', label: 'free to deploy', amount_usd: '98329.57', share_bps: 9832 },
    ],
    ...overrides,
  };
}

/** A money read the backend could not draw: the reason, and no figures. */
export function unavailableAccountMoney(
  detail: string,
  state: AccountMoneyView['state'] = 'unavailable',
): AccountMoneyView {
  return { state, detail, world: 'real_paper', account_id: 'PA-TEST', segments: [] };
}
