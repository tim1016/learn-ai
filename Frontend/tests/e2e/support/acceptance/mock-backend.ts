import type { Page, Request, Route } from '@playwright/test';

/**
 * The mocked backend of the acceptance walks (PRD #3490 section 12, slice 36).
 *
 * Every `/api` request is answered here, whichever host the build is
 * configured to call, so a walk needs no data plane, no database and no broker
 * and can never reach a live service. A request no handler answers gets a 503
 * and is listed in {@link MockBackend.unanswered}; a walk asserts that list is
 * empty (or names what it leaves unanswered on purpose), so a page that starts
 * asking for something new fails the walk instead of silently reading nothing.
 *
 * A write is recorded in {@link MockBackend.sent}. The walks stop before any
 * broker command: {@link MockBackend.commands} lists every write that is not a
 * read the page sends as a POST, and each walk asserts it is empty.
 */

/** One request the page made, decoded. */
export interface ApiCall {
  readonly method: string;
  readonly path: string;
  readonly query: URLSearchParams;
  /** The decoded JSON body of a write; `null` when it carried none. */
  readonly body: unknown;
}

/** How a handler answers: a JSON body (200 unless `status` says otherwise), or an empty event stream. */
export type Answer = { readonly json: unknown; readonly status?: number } | { readonly stream: true };

/** A handler answers the calls it knows and returns `null` for the rest, so the next handler is asked. */
export type Handler = (call: ApiCall) => Answer | null;

export const ok = (json: unknown): Answer => ({ json });

/**
 * POSTs that only read: they compute an answer from their body and change
 * nothing. Anything else that is not a GET is a command.
 */
const READ_SHAPED_POSTS: readonly RegExp[] = [
  /^\/api\/chart\/data$/,
  /^\/api\/research\/return-distribution$/,
  /^\/api\/dataset\/plan$/,
  /^\/api\/research\/options-backtest\/setups\/describe$/,
  /\/bots\/(?:end-preview|budget-preview|admission)$/,
];

const isReadShaped = (call: ApiCall): boolean => READ_SHAPED_POSTS.some((pattern) => pattern.test(call.path));

export class MockBackend {
  /** Every write the page sent, in order. */
  readonly sent: ApiCall[] = [];
  /** Every `/api` request no handler answered, as `METHOD /path`. */
  readonly unanswered: string[] = [];
  /** Every `/api` request, as `METHOD /path?query`, in order. */
  readonly requests: string[] = [];
  private readonly handlers: Handler[] = [];

  /** Adds handlers after the ones already added; the first to answer wins. */
  use(...handlers: Handler[]): this {
    this.handlers.push(...handlers);
    return this;
  }

  /** The writes that are commands: everything not a GET and not a read the page sends as a POST. */
  commands(): ApiCall[] {
    return this.sent.filter((call) => !isReadShaped(call));
  }

  /** Requests whose path ends with the suffix, for asserting what a page asked for. */
  requested(suffix: string): string[] {
    return this.requests.filter((entry) => entry.split('?')[0].endsWith(suffix));
  }

  /**
   * Routes every request of the page. `appOrigin` is the served app, whose own
   * assets load as they are. The page's clock starts at `nowMs` and then runs,
   * so "today" and the last completed session are the same on any day the
   * suite runs, while the app's own timers still tick.
   */
  async install(page: Page, appOrigin: string | undefined, nowMs: number): Promise<void> {
    if (appOrigin === undefined) throw new Error('The walk needs the served app: set baseURL (PLAYWRIGHT_BASE_URL).');
    const origin = new URL(appOrigin).origin;
    await page.clock.install({ time: nowMs });
    await page.route('**/*', (route) => this.answer(route, origin));
  }

  private async answer(route: Route, appOrigin: string): Promise<void> {
    const request = route.request();
    const url = new URL(request.url());
    if (!url.pathname.startsWith('/api/')) {
      // Anything on another host (fonts, icon CDNs) is not part of a hermetic walk.
      if (url.origin === appOrigin) await route.continue();
      else await route.abort();
      return;
    }
    const call = this.decode(request, url);
    this.requests.push(`${call.method} ${call.path}${url.search}`);
    if (call.method !== 'GET') this.sent.push(call);
    for (const handler of this.handlers) {
      const answer = handler(call);
      if (answer === null) continue;
      if ('stream' in answer) {
        await route.fulfill({ status: 200, contentType: 'text/event-stream', headers: { 'Cache-Control': 'no-cache' }, body: '' });
      } else {
        await route.fulfill({ status: answer.status ?? 200, json: answer.json });
      }
      return;
    }
    this.unanswered.push(`${call.method} ${call.path}`);
    await route.fulfill({ status: 503, json: { detail: 'Outside the acceptance walk.' } });
  }

  private decode(request: Request, url: URL): ApiCall {
    const method = request.method();
    let body: unknown = null;
    if (method !== 'GET' && request.postData() !== null) {
      try {
        body = request.postDataJSON() as unknown;
      } catch {
        body = request.postData();
      }
    }
    return { method, path: url.pathname, query: url.searchParams, body };
  }
}
