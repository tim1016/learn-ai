import { Component, signal, provideZonelessChangeDetection } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { MessageService } from 'primeng/api';
import { gqlResource } from './gql-resource';
import { environment } from '../../../environments/environment';

interface PingData { ping: string }

@Component({
  selector: 'app-test-host',
  template: '',
})
class TestHostComponent {
  readonly param = signal<{ id: string } | undefined>(undefined);
  readonly resource = gqlResource<PingData, { id: string }>(
    'query Ping($id: String!) { ping }',
    () => this.param(),
    { errorContext: 'Ping' },
  );
}

/** Yield to the microtask queue so scheduled effects (httpResource loader) can run. */
const flush = (): Promise<void> => new Promise(resolve => queueMicrotask(() => resolve()));

describe('gqlResource', () => {
  let messageService: { add: ReturnType<typeof vi.fn> };
  let http: HttpTestingController;

  beforeEach(() => {
    messageService = { add: vi.fn() };
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [
        provideZonelessChangeDetection(),
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: MessageService, useValue: messageService },
      ],
    });
    http = TestBed.inject(HttpTestingController);
  });

  afterEach(() => {
    http.verify();
  });

  it('skips the request while params() returns undefined', async () => {
    const fixture = TestBed.createComponent(TestHostComponent);
    fixture.detectChanges();
    await flush();

    http.expectNone(environment.backendUrl);
    expect(fixture.componentInstance.resource.value()).toBeUndefined();
  });

  it('fires once params() returns a value and exposes parsed data', async () => {
    const fixture = TestBed.createComponent(TestHostComponent);
    fixture.componentInstance.param.set({ id: 'abc' });
    fixture.detectChanges();
    await flush();

    const req = http.expectOne(environment.backendUrl);
    expect(req.request.method).toBe('POST');
    expect(req.request.body).toEqual({
      query: 'query Ping($id: String!) { ping }',
      variables: { id: 'abc' },
    });
    req.flush({ data: { ping: 'pong' } });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(fixture.componentInstance.resource.value()).toEqual({ ping: 'pong' });
    expect(messageService.add).not.toHaveBeenCalled();
  });

  it('toasts and surfaces a GraphqlError when response.errors is non-empty', async () => {
    const fixture = TestBed.createComponent(TestHostComponent);
    fixture.componentInstance.param.set({ id: 'abc' });
    fixture.detectChanges();
    await flush();

    http.expectOne(environment.backendUrl).flush({ data: null, errors: [{ message: 'rate limited' }] });
    await fixture.whenStable();
    fixture.detectChanges();
    await fixture.whenStable();

    expect(fixture.componentInstance.resource.error()).toBeTruthy();
    expect(messageService.add).toHaveBeenCalledTimes(1);
    const call = messageService.add.mock.calls[0][0];
    expect(call.severity).toBe('error');
    expect(call.summary).toBe('Ping');
    expect(call.detail).toContain('rate limited');
  });

  it('refires when params() changes value', async () => {
    const fixture = TestBed.createComponent(TestHostComponent);
    fixture.componentInstance.param.set({ id: 'a' });
    fixture.detectChanges();
    await flush();

    http.expectOne(environment.backendUrl).flush({ data: { ping: 'one' } });
    await fixture.whenStable();

    fixture.componentInstance.param.set({ id: 'b' });
    fixture.detectChanges();
    await flush();

    const req2 = http.expectOne(environment.backendUrl);
    expect(req2.request.body).toEqual({
      query: 'query Ping($id: String!) { ping }',
      variables: { id: 'b' },
    });
    req2.flush({ data: { ping: 'two' } });
    await fixture.whenStable();
    fixture.detectChanges();

    expect(fixture.componentInstance.resource.value()).toEqual({ ping: 'two' });
  });
});
