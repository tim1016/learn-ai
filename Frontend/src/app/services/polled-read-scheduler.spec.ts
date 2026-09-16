import { HttpClient, HttpParams } from '@angular/common/http';
import { TestBed } from '@angular/core/testing';
import { Subject, type Observable } from 'rxjs';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { POLL_REQUEST_TIMEOUT_MS } from './poll-timeout';
import { PolledReadScheduler } from './polled-read-scheduler';

class FakeHttpClient {
  readonly urls: string[] = [];
  private readonly pending: Subject<unknown>[] = [];

  get<T>(url: string, options?: { params?: HttpParams }): Observable<T> {
    const query = options?.params?.toString();
    this.urls.push(query ? `${url}?${query}` : url);
    const subject = new Subject<T>();
    this.pending.push(subject as Subject<unknown>);
    return subject;
  }

  /** Settle the nth issued request, oldest first. */
  settle(index: number, value: unknown): void {
    const subject = this.pending[index];
    subject.next(value);
    subject.complete();
  }

  fail(index: number, error: unknown): void {
    this.pending[index].error(error);
  }
}

function setup() {
  const http = new FakeHttpClient();
  TestBed.configureTestingModule({
    providers: [{ provide: HttpClient, useValue: http }],
  });
  return { http, scheduler: TestBed.inject(PolledReadScheduler) };
}

/** Let the serialization chain hand the next queued read its turn. */
async function flush(): Promise<void> {
  for (let i = 0; i < 8; i += 1) await Promise.resolve();
}

afterEach(() => {
  TestBed.resetTestingModule();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe('PolledReadScheduler', () => {
  it('issues an uncontended read synchronously, like the call it replaces', () => {
    const { http, scheduler } = setup();

    // Deliberately no await. With nothing in flight there is no convoy to
    // join, so scheduling must not cost a read its synchronous dispatch —
    // that is what keeps every single-read caller behaving as it did.
    void scheduler.get('/api/catalog');

    expect(http.urls).toEqual(['/api/catalog']);
  });

  it('issues one request when the same surface is read twice concurrently', async () => {
    const { http, scheduler } = setup();

    const first = scheduler.get<number>('/api/catalog');
    const second = scheduler.get<number>('/api/catalog');
    await flush();

    expect(http.urls).toEqual(['/api/catalog']);

    http.settle(0, 7);

    expect(await first).toBe(7);
    expect(await second).toBe(7);
  });

  it('goes back to the network once the previous read has settled', async () => {
    const { http, scheduler } = setup();

    const first = scheduler.get<number>('/api/catalog');
    await flush();
    http.settle(0, 1);
    await first;

    const second = scheduler.get<number>('/api/catalog');
    await flush();
    http.settle(1, 2);

    expect(await second).toBe(2);
    expect(http.urls).toEqual(['/api/catalog', '/api/catalog']);
  });

  it('treats differing params as a different surface', async () => {
    const { http, scheduler } = setup();

    void scheduler.get('/api/chart', new HttpParams().set('resolution', '1m'));
    void scheduler.get('/api/chart', new HttpParams().set('resolution', '5m'));
    await flush();

    expect(http.urls).toEqual(['/api/chart?resolution=1m']);

    http.settle(0, null);
    await flush();

    expect(http.urls).toEqual(['/api/chart?resolution=1m', '/api/chart?resolution=5m']);
  });

  it('dispatches different surfaces one at a time, in order', async () => {
    const { http, scheduler } = setup();

    void scheduler.get('/api/catalog');
    void scheduler.get('/api/account');
    void scheduler.get('/api/clerk');
    await flush();

    expect(http.urls).toEqual(['/api/catalog']);

    http.settle(0, null);
    await flush();
    expect(http.urls).toEqual(['/api/catalog', '/api/account']);

    http.settle(1, null);
    await flush();
    expect(http.urls).toEqual(['/api/catalog', '/api/account', '/api/clerk']);
  });

  it('dispatches a concurrent group together, and settles each member on its own', async () => {
    const { http, scheduler } = setup();

    const [a, b, c] = scheduler.getGroup<number>(['/api/lane-a', '/api/lane-b', '/api/lane-c']);
    await flush();

    // All three at once. One at a time is what the default does, and it is
    // what makes one lane's latency another lane's fault (FR-093).
    expect(http.urls).toEqual(['/api/lane-a', '/api/lane-b', '/api/lane-c']);

    http.settle(1, 2);
    expect(await b).toBe(2);

    http.fail(0, new Error('lane a down'));
    await expect(a).rejects.toThrow('lane a down');

    http.settle(2, 3);
    expect(await c).toBe(3);
  });

  it('spends one turn in the shared queue for the whole group', async () => {
    const { http, scheduler } = setup();

    const leading = scheduler.get<number>('/api/catalog');
    const group = scheduler.getGroup<number>(['/api/lane-a', '/api/lane-b']);
    const trailing = scheduler.get<number>('/api/account');
    await flush();

    // The group waits its turn like any other read — it does not jump the
    // queue, so this is not the five-concurrent-pollers case #1801 measured.
    expect(http.urls).toEqual(['/api/catalog']);

    http.settle(0, 1);
    await flush();
    expect(http.urls).toEqual(['/api/catalog', '/api/lane-a', '/api/lane-b']);

    // And the read behind it waits for the WHOLE group, not its first member.
    http.settle(1, 2);
    await flush();
    expect(http.urls).toEqual(['/api/catalog', '/api/lane-a', '/api/lane-b']);

    http.settle(2, 3);
    await flush();
    expect(http.urls).toEqual(['/api/catalog', '/api/lane-a', '/api/lane-b', '/api/account']);

    http.settle(3, 4);
    expect(await leading).toBe(1);
    expect(await group[0]).toBe(2);
    expect(await group[1]).toBe(3);
    expect(await trailing).toBe(4);
  });

  it('joins an already-outstanding surface from inside a group instead of re-issuing it', async () => {
    const { http, scheduler } = setup();

    const solo = scheduler.get<number>('/api/lane-a');
    const [joined, fresh] = scheduler.getGroup<number>(['/api/lane-a', '/api/lane-b']);
    await flush();

    expect(http.urls).toEqual(['/api/lane-a']);

    http.settle(0, 7);
    await flush();

    expect(await solo).toBe(7);
    expect(await joined).toBe(7);
    expect(http.urls).toEqual(['/api/lane-a', '/api/lane-b']);

    http.settle(1, 8);
    expect(await fresh).toBe(8);
  });

  it('does not wedge the queue when a read fails', async () => {
    const { http, scheduler } = setup();

    const failing = scheduler.get('/api/catalog');
    const following = scheduler.get<number>('/api/account');
    await flush();

    http.fail(0, new Error('boom'));
    await expect(failing).rejects.toThrow('boom');
    await flush();

    expect(http.urls).toEqual(['/api/catalog', '/api/account']);
    http.settle(1, 3);
    expect(await following).toBe(3);
  });

  it('counts queue wait against the poll ceiling and abandons the read', async () => {
    vi.useFakeTimers();
    const { http, scheduler } = setup();

    const blocking = scheduler.get('/api/catalog');
    const queued = scheduler.get('/api/account');
    await flush();
    expect(http.urls).toEqual(['/api/catalog']);

    // Nothing settles: the leading read exhausts the ceiling, and by the time
    // the queued read gets its turn its own ceiling has elapsed too.
    await vi.advanceTimersByTimeAsync(POLL_REQUEST_TIMEOUT_MS + 1);

    await expect(blocking).rejects.toThrow();
    await expect(queued).rejects.toThrow(/abandoned/);
    expect(http.urls).toEqual(['/api/catalog']);
  });

  it('releases the surface after a failure so the next tick can retry it', async () => {
    const { http, scheduler } = setup();

    const failing = scheduler.get('/api/catalog');
    await flush();
    http.fail(0, new Error('boom'));
    await expect(failing).rejects.toThrow('boom');
    await flush();

    void scheduler.get('/api/catalog');
    await flush();

    expect(http.urls).toEqual(['/api/catalog', '/api/catalog']);
  });
});
