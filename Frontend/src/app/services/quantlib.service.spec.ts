import { TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { QuantLibService } from './quantlib.service';
import { environment } from '../../environments/environment';

const PRICE_URL = `${environment.pythonServiceUrl}/api/quantlib/price`;

const OPTION = { spot: 100, strike: 100, volatility: 0.2, expirationMs: 4_070_984_400_000, optionType: 'call' } as const;

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

  // The options-chain engine column reprices without a rate: it is left out of
  // the body (Python rejects a null), so Python fills its one default (#2764).
  it('priceOption sends no rate unless given one, and names every field the way Python does', async () => {
    const pending = service.priceOption(OPTION);

    const req = httpMock.expectOne(PRICE_URL);
    expect(req.request.method).toBe('POST');
    expect(req.request.body.risk_free_rate).toBeUndefined();
    expect(req.request.body).toEqual({
      spot: 100,
      strike: 100,
      volatility: 0.2,
      expiration_ms: 4_070_984_400_000,
      option_type: 'call',
      dividend_yield: 0,
      engine: 'analytic_bs',
    });
    req.flush({ success: true, engine: 'analytic_bs', price: 4.2, delta: 0.5, gamma: 0.05, theta: -0.03, vega: 0.2, rho: 0.1, d1: 0.1, d2: 0 });
    expect(await pending).toMatchObject({ success: true, price: 4.2, delta: 0.5, gamma: 0.05 });
  });

  it('priceOption prices with the rate and the engine it was given', async () => {
    service.selectedEngine.set('binomial_crr');
    const pending = service.priceOption({ ...OPTION, riskFreeRate: 0.05, engine: 'monte_carlo' });

    const req = httpMock.expectOne(PRICE_URL);
    expect(req.request.body).toMatchObject({ risk_free_rate: 0.05, engine: 'monte_carlo' });
    req.flush({ success: true, engine: 'monte_carlo', price: 4.2, delta: 0.5, gamma: 0.05, theta: -0.03, vega: 0.2, rho: 0.1 });
    await pending;
  });

  it('priceOption prices with the selected engine unless given another', async () => {
    service.selectedEngine.set('finite_diff');
    const pending = service.priceOption(OPTION);

    const req = httpMock.expectOne(PRICE_URL);
    expect(req.request.body.engine).toBe('finite_diff');
    req.flush({ success: true, engine: 'finite_diff', price: 4.2, delta: 0.5, gamma: 0.05, theta: -0.03, vega: 0.2, rho: 0.1 });
    await pending;
  });

  it('priceOption resolves success: false with the service\'s reason when Python answers an HTTP failure', async () => {
    const pending = service.priceOption(OPTION);

    httpMock.expectOne(PRICE_URL).flush(
      { detail: [{ loc: ['body', 'volatility'], msg: 'Input should be greater than 0', type: 'greater_than' }] },
      { status: 422, statusText: 'Unprocessable Entity' },
    );

    expect(await pending).toMatchObject({
      success: false, engine: 'analytic_bs', error: 'Input should be greater than 0',
    });
  });
});
