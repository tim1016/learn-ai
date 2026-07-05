import { provideZonelessChangeDetection } from '@angular/core';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import type { DataPlaneHealth } from '../../../../../api/broker-models';
import { PolygonDataEngineCardComponent } from './polygon-data-engine-card.component';

function health(): DataPlaneHealth {
  return {
    service: 'polygon-data-service',
    code_revision: '8398d285978a94d9714490e002962e365e9cd505',
    process_start_ms: 1_780_000_100_000,
    fetched_at_ms: 1_780_000_200_000,
    reload: 'watchfiles-polling',
  };
}

describe('PolygonDataEngineCardComponent', () => {
  it('renders the polygon data engine identity and runtime health', async () => {
    await render(PolygonDataEngineCardComponent, {
      inputs: { health: health() },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByRole('img', { name: /animated polygon database engine icon/i })).toBeTruthy();
    expect(screen.getByRole('heading', { name: 'Polygon Data Engine' })).toBeTruthy();
    expect(screen.getByText('polygon-data-service')).toBeTruthy();
    expect(screen.getByText('8398d285978a')).toBeTruthy();
    expect(screen.getByText('watchfiles-polling')).toBeTruthy();
    expect(screen.queryByText(/data plane/i)).toBeNull();
  });

  it('renders a renamed loading state while health is unavailable', async () => {
    await render(PolygonDataEngineCardComponent, {
      inputs: { health: null, loading: true },
      providers: [provideZonelessChangeDetection()],
    });

    expect(screen.getByText('Loading Polygon data engine health...')).toBeTruthy();
  });
});
