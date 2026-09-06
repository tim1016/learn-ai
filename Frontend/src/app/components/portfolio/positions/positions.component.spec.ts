import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { Component, provideZonelessChangeDetection, signal } from '@angular/core';
import { ComponentFixture, TestBed } from '@angular/core/testing';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { environment } from '../../../../environments/environment';
import type { Position } from '../../../graphql/portfolio-types';
import { PositionsComponent } from './positions.component';

const GRAPHQL_URL = environment.backendUrl;

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

@Component({
  imports: [PositionsComponent],
  template: '<app-positions [accountId]="accountId()" />',
})
class HostComponent {
  readonly accountId = signal('acc-1');
}

describe('PositionsComponent', () => {
  let fixture: ComponentFixture<HostComponent>;
  let httpMock: HttpTestingController;

  beforeEach(async () => {
    TestBed.configureTestingModule({
      imports: [HostComponent],
      providers: [provideZonelessChangeDetection(), provideHttpClient(), provideHttpClientTesting()],
    });
    fixture = TestBed.createComponent(HostComponent);
    httpMock = TestBed.inject(HttpTestingController);
    await fixture.whenStable();
    httpMock.expectOne(GRAPHQL_URL).flush({ data: { getPositions: POSITIONS } });
    await fixture.whenStable();
  });

  afterEach(() => httpMock.verify());

  const rows = (): string[] =>
    Array.from(fixture.nativeElement.querySelectorAll('tbody tr') as NodeListOf<HTMLElement>)
      .map((row) => row.textContent?.replace(/\s+/g, ' ').trim() ?? '')
      .filter((text) => text.length > 0);

  it('lists the open position the API serializes as OPEN', () => {
    // Before #1973 the filter compared against 'Open', so an account with an
    // open position rendered "No positions found."
    expect(fixture.nativeElement.textContent).not.toContain('No positions found');
    expect(rows()).toHaveLength(1);
    expect(rows()[0]).toContain('SPY');
    expect(fixture.nativeElement.querySelector('.status-badge.open')).not.toBeNull();
  });

  it('reveals the CLOSED position when Show closed is ticked', async () => {
    (fixture.nativeElement.querySelector('input[type=checkbox]') as HTMLInputElement).click();
    await fixture.whenStable();

    expect(rows()).toHaveLength(2);
    expect(fixture.nativeElement.querySelector('.status-badge.closed')).not.toBeNull();
  });
});
