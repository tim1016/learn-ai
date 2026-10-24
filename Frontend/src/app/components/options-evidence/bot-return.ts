/** The one in-app route a piece of evidence may offer a way back to: a bot's own page. Anything else offers none. */
const BOT_PAGE = /^\/brokers\/alpaca\/clerks\/[^/?#]+\/accounts\/[^/?#]+\/bots\/[^/?#]+$/;

export function botReturnPath(path: string | undefined | null): string | null {
  return path !== undefined && path !== null && BOT_PAGE.test(path) ? path : null;
}
