import { ComponentFixture, TestBed } from '@angular/core/testing';
import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { Router } from '@angular/router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { StrategyRunsComponent } from './strategy-runs.component';
import { StrategyRunsService } from '../../../services/strategy-runs.service';
import { makeRunLedger, makeRunResponse } from '../../../testing/strategy-run-fixtures';

interface ServiceMock {
  listRuns: ReturnType<typeof vi.fn>;
  runSpyEmaFixture: ReturnType<typeof vi.fn>;
  getRun: ReturnType<typeof vi.fn>;
  createRun: ReturnType<typeof vi.fn>;
}

describe('StrategyRunsComponent', () => {
  let component: StrategyRunsComponent;
  let fixture: ComponentFixture<StrategyRunsComponent>;
  let service: ServiceMock;
  let routerNavigate: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    service = {
      listRuns: vi.fn().mockResolvedValue({ runs: [makeRunLedger()] }),
      runSpyEmaFixture: vi.fn().mockResolvedValue(makeRunResponse()),
      getRun: vi.fn(),
      createRun: vi.fn(),
    };
    routerNavigate = vi.fn().mockResolvedValue(true);

    await TestBed.configureTestingModule({
      imports: [StrategyRunsComponent],
      providers: [
        provideHttpClient(),
        provideHttpClientTesting(),
        { provide: StrategyRunsService, useValue: service },
        { provide: Router, useValue: { navigate: routerNavigate } },
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(StrategyRunsComponent);
    component = fixture.componentInstance;
    // Wait for the constructor's initial refresh() to settle.
    await fixture.whenStable();
    fixture.detectChanges();
  });

  afterEach(() => {
    vi.clearAllMocks();
  });

  it('loads runs on construction and renders each run symbol through the shared asset identity', () => {
    expect.soft(service.listRuns, 'loads runs from the service on construction: listRuns call').toHaveBeenCalledOnce();
    expect.soft(component.runs(), 'loads runs from the service on construction: run count').toHaveLength(1);
    expect.soft(component.runs()[0]?.symbol, 'loads runs from the service on construction: run symbol').toBe('SPY');

    const identity = fixture.nativeElement.querySelector('app-asset-identity');

    expect.soft(identity, 'renders each run symbol through the shared asset identity: present').not.toBeNull();
    expect
      .soft(identity.textContent, 'renders each run symbol through the shared asset identity: carries SPY')
      .toContain('SPY');
  });

  it('refresh() re-issues the listRuns call', async () => {
    service.listRuns.mockClear();
    service.listRuns.mockResolvedValueOnce({ runs: [] });
    await component.refresh();
    expect(service.listRuns).toHaveBeenCalledOnce();
    expect(component.runs()).toEqual([]);
  });

  it('runFixture() POSTs the fixture, then refreshes, and remembers the new run_id', async () => {
    service.listRuns.mockClear();
    await component.runFixture();
    expect(service.runSpyEmaFixture).toHaveBeenCalledOnce();
    expect(service.listRuns).toHaveBeenCalledOnce();
    expect(component.lastFixtureRunId()).toBe(
      'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    );
  });

  it('open(run) navigates to the detail route', () => {
    const ledger = makeRunLedger({ run_id: 'b'.repeat(32) });
    component.open(ledger);
    expect(routerNavigate).toHaveBeenCalledWith([
      '/research/history/spec-run',
      'b'.repeat(32),
    ]);
  });

  it('surfaces service errors without throwing', async () => {
    service.listRuns.mockRejectedValueOnce(new Error('boom'));
    await component.refresh();
    expect(component.error()).toBe('boom');
    expect(component.runs()).toHaveLength(1); // last successful state preserved
  });

  describe('statusSeverity', () => {
    it('maps every status to a severity token', () => {
      expect(component.statusSeverity('completed')).toBe('success');
      expect(component.statusSeverity('running')).toBe('info');
      expect(component.statusSeverity('failed')).toBe('danger');
    });
  });

  describe('shortHash', () => {
    it('returns "—" for nullish input and truncates to the requested length', () => {
      expect.soft(component.shortHash(null), 'returns "—" for nullish input: null').toBe('—');
      expect.soft(component.shortHash(''), 'returns "—" for nullish input: empty string').toBe('—');
      expect.soft(component.shortHash('abcdefghij', 4), 'truncates to the requested length').toBe('abcd');
    });
  });
});
