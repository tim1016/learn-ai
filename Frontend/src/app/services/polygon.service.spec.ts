import { TestBed } from '@angular/core/testing';
import { vi } from 'vitest';
import { PolygonService } from './polygon.service';

// Mock the environment to avoid requiring real API keys
vi.mock('../../environments/environment', () => ({
  environment: {
    polygonApiKey: '',
    useBackendProxy: true,
  },
}));

describe('PolygonService', () => {
  let service: PolygonService;

  beforeEach(() => {
    TestBed.resetTestingModule();
    TestBed.configureTestingModule({});
    service = TestBed.inject(PolygonService);
  });

  it('should be created', () => {
    expect(service).toBeTruthy();
  });

  it('should not initialize client when using backend proxy', () => {
    // Client is null when useBackendProxy is true
    expect(() => service.getStockAggregates({
      ticker: 'AAPL',
      multiplier: 1,
      timespan: 'day',
      from: '2026-01-01',
      to: '2026-01-31',
    })).rejects.toThrow('Polygon API client not initialized');
  });

  it('should throw descriptive error when client not initialized', async () => {
    await expect(service.getLastTrade('AAPL')).rejects.toThrow(
      'Either set polygonApiKey in environment or enable backend proxy'
    );
  });
});
