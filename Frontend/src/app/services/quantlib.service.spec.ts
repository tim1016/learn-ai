import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { QuantLibService } from './quantlib.service';
import { environment } from '../../environments/environment';

const GRAPHQL_URL = environment.backendUrl;

describe('QuantLibService', () => {
  let service: QuantLibService;
  let httpMock: HttpTestingController;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({
      providers: [provideHttpClient(), provideHttpClientTesting()],
    });
    service = TestBed.inject(QuantLibService);
    httpMock = TestBed.inject(HttpTestingController);
  });

  afterEach(() => httpMock.verify());

  // The options-chain engine column reprices without a rate: it travels as
  // null, so the Backend omits it and Python fills its one default (#2764).
  it('priceOption sends no rate unless given one', async () => {
    const pending = service.priceOption({
      spot: 100, strike: 100, volatility: 0.2, expirationDate: '2099-01-01', optionType: 'call',
    });

    const req = httpMock.expectOne(GRAPHQL_URL);
    expect(req.request.body.variables.riskFreeRate).toBeNull();
    req.flush({ data: { quantlibPrice: { success: true } } });
    await pending;
  });
});
