import { HttpClient, type HttpParams } from '@angular/common/http';
import { Injectable, inject } from '@angular/core';
import { type Observable, firstValueFrom, timeout } from 'rxjs';

import { POLL_REQUEST_TIMEOUT_MS } from './poll-timeout';

/**
 * The one scheduler every polled panel/catalog read goes through (#1912).
 *
 * #1801 measured these reads fully serializing on the server: at 144 roster
 * rows a lone catalog read costs 267 ms and six concurrent ones cost 2.58 s
 * *each* — 8.78 s with fifty bots trading. The client is what supplies that
 * concurrency. The roster route alone lands five independent pollers in the
 * same tick (catalog on 5 s; account, clerk, panel and tape on 15 s), which
 * is why an operator's browser reliably reproduces the storm while anyone
 * checking by hand issues one request and sees the fast read.
 *
 * Two rules, and between them the browser stops producing the concurrent
 * case:
 *
 * - **Single-flight by surface.** A read whose key is already outstanding
 *   joins the existing promise instead of issuing a second request. Nothing
 *   is retained past the response, so this is not a cache and does not
 *   introduce a second freshness authority next to the sweep (#1776) — a
 *   read that arrives after the first settles goes to the network like any
 *   other.
 * - **One read at a time.** Reads queue and dispatch in order, so five
 *   pollers cost roughly five sequential reads rather than five concurrent
 *   ones. Serializing here is what makes each individual read fast; the
 *   server-side lever (reducing per-read work) is #1801's and lands
 *   separately.
 *
 * This deliberately does not tune `POLL_REQUEST_TIMEOUT_MS`. The ceiling is
 * not wrong; the read is slow.
 */
@Injectable({ providedIn: 'root' })
export class PolledReadScheduler {
  private readonly http = inject(HttpClient);

  /**
   * Reads handed to a caller but not yet settled, keyed by surface. Doubles
   * as the queue depth: empty means nothing is in flight.
   */
  private readonly outstanding = new Map<string, Promise<unknown>>();

  /**
   * Tail of the serialized chain — the next read waits on this before it
   * dispatches. Always resolves (never rejects), so one failed read cannot
   * wedge every read queued behind it.
   */
  private tail: Promise<void> = Promise.resolve();

  /** GET `url` under the poll ceiling, coalesced and serialized by surface. */
  get<T>(url: string, params?: HttpParams): Promise<T> {
    const query = params?.toString();
    return this.run(query ? `${url}?${query}` : url, () =>
      this.http.get<T>(url, { params }),
    );
  }

  private run<T>(key: string, request: () => Observable<T>): Promise<T> {
    const joined = this.outstanding.get(key);
    // The map is keyed by request URL, which is what determines the response
    // shape, so the joined read's type is this caller's type.
    if (joined !== undefined) return joined as Promise<T>;

    // The ceiling starts here, at enqueue, not at dispatch: its job is to
    // bound how long one poll can leave the surface frozen (S7), and time
    // spent waiting for a turn freezes the surface exactly as much as time
    // spent waiting for the server.
    const deadlineAtMs = Date.now() + POLL_REQUEST_TIMEOUT_MS;
    const send = (): Promise<T> => dispatch(request, deadlineAtMs);
    // With nothing outstanding there is no convoy to join, so the read goes
    // straight out rather than a microtask later. That keeps an uncontended
    // read — an operator opening a bot, a lone hand-run request — identical
    // to the unscheduled call it replaces, and confines every behavioural
    // change to the overlapping case this scheduler exists for.
    const scheduled = this.outstanding.size === 0 ? send() : this.tail.then(send);

    this.outstanding.set(key, scheduled);
    const settled = (): void => {
      this.outstanding.delete(key);
    };
    this.tail = scheduled.then(settled, settled);
    return scheduled;
  }
}

/**
 * Issue one queued read against whatever is left of the poll ceiling.
 *
 * A read whose turn arrives after the ceiling has already passed is
 * abandoned rather than issued: the tick that wanted it is long gone, the
 * next one will ask again, and issuing it anyway would put load on the
 * server for a result no caller is still waiting on.
 */
function dispatch<T>(request: () => Observable<T>, deadlineAtMs: number): Promise<T> {
  const remainingMs = deadlineAtMs - Date.now();
  if (remainingMs <= 0) {
    return Promise.reject(
      new Error('Polled read abandoned: the poll ceiling elapsed while it waited for a turn.'),
    );
  }
  return firstValueFrom(request().pipe(timeout({ first: remainingMs })));
}
