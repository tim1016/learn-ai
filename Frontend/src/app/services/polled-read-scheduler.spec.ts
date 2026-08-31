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
