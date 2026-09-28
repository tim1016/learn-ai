import { refusalBody } from '../../../../shared/errors/refusal-body';
import { extractServerMessage } from '../../operation-error';
import type { AccountMoneyView } from './broker-v2-panel.service';

/** What a money surface shows in place of figures it cannot draw: one line,
 * and the next step when there is one. */
interface NoMoney {
  readonly text: string;
  readonly nextStep: string | null;
}

/**
 * The one answer to "can this account's money be drawn, and if not, why?"
 * (PRD #2560 D12). Every money surface — the Accounts page card, the
 * workspace header, Home's bar, Deploy's Money step, the bot page — renders
 * this and nothing else, so they cannot word one account's state two ways.
 *
 * - `ready` — the backend drew the money; render `view` as given.
 * - `loading` — no answer yet.
 * - `incapable` — the account's lane does not serve the money read at all, so
 *   there is nothing to wait for and nothing that failed.
 * - `unavailable` — the backend answered that it cannot draw the money (a
 *   missing loss limit, budgets not switched on: `view` is that answer and
 *   `text` its own reason), or the read itself failed (`view` is `null`, and
 *   `text` / `nextStep` are the backend's refusal when it authored one).
 *
 * An unknown is never shown as $0: every state that is not `ready` carries a
 * sentence instead of a figure.
 */
export type AccountMoneyState =
  | { readonly kind: 'ready'; readonly view: AccountMoneyView }
  | ({ readonly kind: 'loading' | 'incapable' } & NoMoney)
  | ({ readonly kind: 'unavailable'; readonly view: AccountMoneyView | null } & NoMoney);

/** One account-money read, as its `resource()` reports it. */
export interface AccountMoneyRead {
  /** Whether the account's lane declares `bot_panel_read`, the capability the
   * read is served under; `null` while no lane has been resolved. */
  readonly capable: boolean | null;
  /** The read's latest answer. A resource keeps it across a `reload()`, so a
   * poll never blanks the figures it is refreshing. */
  readonly view: AccountMoneyView | undefined;
  /** Why the read failed, when it did. */
  readonly error: unknown;
}

export const MONEY_LOADING_TEXT = 'Reading account money…';
export const MONEY_INCAPABLE_TEXT = 'This account does not report its money.';
export const MONEY_FAILED_TEXT = 'Account money could not be read.';
/** Said only when the refusal authored no next step of its own: the surfaces
 * that show this re-read on a timer, so waiting is itself the next step. */
export const MONEY_FAILED_NEXT_STEP = 'It is read again automatically; if this persists, open the account’s Settings.';

export function accountMoneyState(read: AccountMoneyRead): AccountMoneyState {
  if (read.capable === false) return { kind: 'incapable', text: MONEY_INCAPABLE_TEXT, nextStep: null };
  const view = read.view;
  if (view !== undefined) {
    return view.state === 'ready'
      ? { kind: 'ready', view }
      : { kind: 'unavailable', view, text: view.detail, nextStep: null };
  }
  if (read.error === undefined || read.error === null) {
    return { kind: 'loading', text: MONEY_LOADING_TEXT, nextStep: null };
  }
  return { kind: 'unavailable', view: null, ...failedRead(read.error) };
}

/** A failed read in the backend's own words: the refusal's message and why
 * (FastAPI's `{detail: {message, why, next_action}}`) or the fleet's flat
 * `{message, next_step}`, read through the shared refusal parsers. */
function failedRead(error: unknown): NoMoney {
  const refusal = refusalBody(error);
  const message = extractServerMessage(error, MONEY_FAILED_TEXT);
  const why = stringField(refusal, 'why');
  return {
    text: why === null || why === message ? message : `${message} ${why}`,
    nextStep: stringField(refusal, 'next_action') ?? stringField(refusal, 'next_step') ?? MONEY_FAILED_NEXT_STEP,
  };
}

function stringField(body: Record<string, unknown> | null, key: string): string | null {
  const value = body?.[key];
  return typeof value === 'string' && value.trim().length > 0 ? value : null;
}
