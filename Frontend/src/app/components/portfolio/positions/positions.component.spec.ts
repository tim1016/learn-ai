import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import type { Position } from '../../../graphql/portfolio-types';
import { PositionsComponent } from './positions.component';

/** Positions exactly as the .NET API serializes them: enum members upper-cased (#1973). */
const POSITIONS: Position[] = [
  {
    id: 'pos-open', accountId: 'acc-1', tickerId: 1, assetType: 'STOCK',
    netQuantity: 2, avgCostBasis: 500, realizedPnL: 0, status: 'OPEN',
    openedAt: '2026-09-06T22:35:00.087Z', ticker: { symbol: 'SPY', name: 'SPY' },
  },
  {
    id: 'pos-closed', accountId: 'acc-1', tickerId: 2, assetType: 'STOCK',
    netQuantity: 0, avgCostBasis: 200, realizedPnL: 500, status: 'CLOSED',
    openedAt: '2026-01-15T15:00:00Z', closedAt: '2026-02-15T15:00:00Z', ticker: { symbol: 'MSFT', name: 'Microsoft' },
  },
];

async function renderPositions() {
  const view = await render(PositionsComponent, {
    inputs: { accountId: 'acc-1' },
    providers: [provideHttpClient(), provideHttpClientTesting()],
  });
  const httpMock = TestBed.inject(HttpTestingController);
  httpMock.expectOne(environment.backendUrl).flush({ data: { getPositions: POSITIONS } });
  await view.fixture.whenStable();
  return { view, httpMock };
}

describe('PositionsComponent', () => {
  afterEach(() => TestBed.inject(HttpTestingController).verify());

  it('lists the open position the API serializes as OPEN', async () => {
    // Before #1973 the filter compared against 'Open', so an account with an
    // open position rendered "No positions found."
    await renderPositions();

    expect(screen.queryByText('No positions found.')).toBeNull();
    const rows = screen.getAllByRole('row');
    expect(rows).toHaveLength(2); // header + the open position
    expect(rows[1].textContent).toContain('SPY');
    expect(screen.getByText('OPEN').className).toContain('open');
  });

  it('reveals the CLOSED position when Show closed is ticked', async () => {
    const { view } = await renderPositions();

    await userEvent.click(screen.getByLabelText('Show closed'));
    await view.fixture.whenStable();

    expect(screen.getAllByRole('row')).toHaveLength(3);
    expect(screen.getByText('CLOSED').className).toContain('closed');
  });
});
