import { provideHttpClient } from '@angular/common/http';
import {
  HttpTestingController,
  provideHttpClientTesting,
} from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { BrokersService } from './brokers.service';

describe('BrokersService', () => {
  let service: BrokersService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting(), BrokersService],
    });
    service = TestBed.inject(BrokersService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  it('GETs the account for the named broker', async () => {
    const promise = service.getAccount('alpaca');

    const req = httpMock.expectOne('/api/brokers/alpaca/account');
    expect(req.request.method).toBe('GET');
    req.flush({ account_id: 'PA1' });

    await expect(promise).resolves.toMatchObject({ account_id: 'PA1' });
  });

  it('defaults the broker to alpaca', async () => {
    const promise = service.getAccount();

    httpMock.expectOne('/api/brokers/alpaca/account').flush({ account_id: 'PA1' });

    await promise;
  });
});
