import type { AccountMoneyView, AccountPictureView } from '../components/broker/v2-panel/lib/broker-v2-panel.service';

/** One ready account-money read, exactly as the backend authors it (every
 * dollar string and width below was produced by
 * `app/broker/alpaca/clerk/account_money.py` for a $100,000 Paper account
 * with one running bot, one stopped bot still holding 1 SPY, and a $0.01
 * fee Alpaca has not taken yet). The stopped bot registered first, so it has
 * palette slot 0; nothing is short, so no shortfall is sent. */
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
    settling_usd: '0.00',
    stopped_holding_count: 1,
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
          held_label: 'in shares',
          in_shares_usd: '764.71', in_shares_bps: 7647,
          pending_usd: '0.00', pending_bps: 0,
          free_usd: '235.28', free_bps: 2353,
        },
        palette_index: 1,
      },
      {
        kind: 'stopped',
        strategy_instance_id: 'spy-ema-20260925-1402',
        label: 'held by stopped bot spy-ema-20260925-1402',
        amount_usd: '670.43',
        share_bps: 67,
        released_usd: '0.00',
        still_claimed_usd: '0.00',
        palette_index: 0,
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

/** The picture of the account `fakeAccountMoney` is, exactly as the backend authors it
 * (`app/broker/alpaca/clerk/account_picture.py`): $98,564.86 of cash with the running bot's $235.28 free and
 * a $0.01 fee not posted yet, so $98,329.57 unreserved, which is `fakeAccountMoney`'s free to deploy. No bot
 * holds an option, so nothing is concentrated and nothing is owed. */
export function fakeAccountPicture(overrides: Partial<AccountPictureView> = {}): AccountPictureView {
  const books = { state: 'known', as_of_ms: 1_700_000_000_000 } as const;
  return {
    owned: {
      broker_cash: {
        ...books, amount_usd: '98564.86', source: "Alpaca's account answer",
        terms: [{ label: 'Cash as the account reports it', amount_usd: '98564.86' }],
      },
      marked_equity: {
        ...books, amount_usd: '100012.40', source: "Alpaca's account answer",
        terms: [{ label: 'Equity as the account reports it', amount_usd: '100012.40' }],
      },
      note: 'Marked equity is cash plus positions at their marks. Only cash can be spent; equity never is.',
    },
    spoken_for: {
      partition: {
        ...books, amount_usd: '98564.86', source: 'Clerk books and the account reading, partitioned',
        terms: [
          { label: 'Set aside for running bots', amount_usd: '235.28' },
          { label: 'Collateral held against sold options', amount_usd: '0.00' },
          { label: 'Held in entry orders', amount_usd: '0.00' },
          { label: 'Outside purchases not in the cash yet', amount_usd: '0.00' },
          { label: 'Fees not posted yet', amount_usd: '0.01' },
          { label: 'Unreserved cash', amount_usd: '98329.57' },
        ],
      },
      lines: [
        {
          kind: 'bot_free', label: 'Set aside for running bots',
          figure: {
            ...books, amount_usd: '235.28',
            source: "Clerk books: each running bot's budget less what it holds and has working",
            terms: [{ label: 'Bot spy-ema-20260929-0931', amount_usd: '235.28' }],
          },
        },
        {
          kind: 'collateral', label: 'Collateral held against sold options',
          figure: {
            ...books, amount_usd: '0.00', terms: [],
            source: 'Clerk books: the requirement of the contracts each bot holds, bot by bot, which the bots set '
              + 'aside independently of one another',
          },
        },
        {
          kind: 'orders', label: 'Held in entry orders',
          figure: {
            ...books, amount_usd: '0.00', terms: [],
            source: "Clerk books and the account reading: each entry order's unfilled cost and fee provision, and "
              + 'fills the cash reading has not shown yet',
          },
        },
        {
          kind: 'outside', label: 'Outside purchases not in the cash yet',
          figure: {
            ...books, amount_usd: '0.00', terms: [],
            source: 'Clerk books and the account reading: purchases by manual trades and by orders placed outside '
              + 'the system that the cash reading has not shown yet',
          },
        },
        {
          kind: 'fees', label: 'Fees not posted yet',
          figure: {
            ...books, amount_usd: '0.01',
            source: 'Clerk books and the account reading: fees the cash reading has not shown yet, which the books '
              + 'keep for the account as one figure',
            terms: [{ label: 'Fees not posted yet', amount_usd: '0.01' }],
          },
        },
        {
          kind: 'unreserved', label: 'Unreserved cash',
          figure: {
            ...books, amount_usd: '98329.57',
            source: 'Clerk books and the account reading: the cash less every claim above',
            terms: [
              { label: 'Cash as the account reports it', amount_usd: '98564.86' },
              { label: 'Set aside for running bots', amount_usd: '-235.28' },
              { label: 'Fees not posted yet', amount_usd: '-0.01' },
            ],
          },
        },
      ],
      sentence: "These lines add up to the account's cash, to the cent.",
    },
    concentration: {
      account_collateral: {
        ...books, amount_usd: '0.00', terms: [],
        source: "Alpaca's published rule over every bot's contracts together, the largest expiry of each "
          + "underlying; the bots set aside their own collateral one by one, which can add up to more",
      },
      groups: [],
      expiry_note: null,
    },
    obligations: {
      assignment: {
        ...books, amount_usd: '0.00', terms: [],
        source: "The option contracts the books attribute to this account's bots, stopped bots included: each sold "
          + 'contract at its strike and multiplier, with nothing netted and no premium',
      },
      assignment_note: 'The assignment obligation is the value, at the strikes, of the shares a sold option could '
        + 'oblige the account to buy or deliver. It is gross and is not a loss: a bounded loss is not a bounded '
        + 'obligation.',
      close_everything: {
        ...books, amount_usd: '0.00', terms: [],
        source: 'No bot holds an option contract, so there is nothing to close',
      },
      bots_holding: 0,
      bots_unpriced: 0,
      conditions: ['Amounts are before fees. A positive amount is paid and a negative one comes back.'],
    },
    note: 'These figures inform. Nothing here blocks an entry or sets a limit.',
    ...overrides,
  };
}
