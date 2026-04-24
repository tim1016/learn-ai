import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { MessageService } from 'primeng/api';
import { GqlClient } from './gql-client';
import { GraphqlError } from './graphql-error';
import { environment } from '../../../environments/environment';

describe('GqlClient', () => {
  let client: GqlClient;
  let http: HttpTestingController;
  let messageService: { add: ReturnType<typeof vi.fn> };

  beforeEach(() => {
    messageService = { add: vi.fn() };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: MessageService, useValue: messageService },
        GqlClient,
      ],
    });
    client = TestBed.inject(GqlClient);
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
  });

  it('returns the data field on a successful response', async () => {
    const promise = client.post<{ ping: string }>('query Q { ping }');

    const req = http.expectOne(environment.backendUrl);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({ query: 'query Q { ping }', variables: {} });
    req.flush({ data: { ping: 'pong' } });

    await expect(promise).resolves.toEqual({ ping: 'pong' });
    expect(messageService.add).not.toHaveBeenCalled();
  });

  it('throws GraphqlError and toasts when response.errors is non-empty', async () => {
    const promise = client.post('query Q { ping }', undefined, { errorContext: 'Ping query' });

    const req = http.expectOne(environment.backendUrl);
    req.flush({ data: null, errors: [{ message: 'rate limited' }, { message: 'try again' }] });

    await expect(promise).rejects.toBeInstanceOf(GraphqlError);
    expect(messageService.add).toHaveBeenCalledTimes(1);
    const call = messageService.add.mock.calls[0][0];
    expect(call.severity).toBe('error');
    expect(call.summary).toBe('Ping query');
    expect(call.detail).toContain('rate limited');
    expect(call.detail).toContain('try again');
  });

  it('toasts a network error when the HTTP layer fails', async () => {
    const promise = client.post('query Q { ping }');

    const req = http.expectOne(environment.backendUrl);
    req.error(new ProgressEvent('Network down'), { status: 503, statusText: 'Service Unavailable' });

    await expect(promise).rejects.toBeTruthy();
    expect(messageService.add).toHaveBeenCalledTimes(1);
    const call = messageService.add.mock.calls[0][0];
    expect(call.severity).toBe('error');
    expect(call.summary).toBe('Network error');
    expect(call.detail).toContain('503');
  });

  it('respects suppressToast', async () => {
    const promise = client.post('query Q { ping }', undefined, { suppressToast: true });

    const req = http.expectOne(environment.backendUrl);
    req.flush({ data: null, errors: [{ message: 'boom' }] });

    await expect(promise).rejects.toBeInstanceOf(GraphqlError);
    expect(messageService.add).not.toHaveBeenCalled();
  });

  it('forwards variables in the request body', async () => {
    const promise = client.post<{ ok: boolean }, { ticker: string; limit: number }>(
      'query Q($ticker: String!, $limit: Int!) { ok }',
      { ticker: 'AAPL', limit: 10 },
    );

    const req = http.expectOne(environment.backendUrl);
    expect(req.request.body).toEqual({
      query: 'query Q($ticker: String!, $limit: Int!) { ok }',
      variables: { ticker: 'AAPL', limit: 10 },
    });
    req.flush({ data: { ok: true } });

    await expect(promise).resolves.toEqual({ ok: true });
  });
});
