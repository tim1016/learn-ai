import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { of, throwError } from 'rxjs';

import { ResearchService } from '../../../services/research.service';
import { ExperimentHistoryComponent } from './experiment-history.component';
import { SymbolPickerComponent } from '../../../shared/symbol-picker/symbol-picker.component';
import type { TickerOption } from '../../../shared/ticker-range-picker/ticker-range-picker.types';
import {
  fakeEnsureCoverage,
  fakeVendorCatalog,
  provideFakeEnsureCoverage,
  provideFakeVendorCatalog,
} from '../../../shared/symbol-catalog/testing/fake-symbol-catalog';
import {
  fakeTickerCatalog,
  provideFakeTickerCatalog,
} from '../../../shared/ticker-catalog/testing/fake-ticker-catalog';

const PICKER_POOL: readonly TickerOption[] = [
  { symbol: 'AAPL', name: 'Apple Inc.', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
  { symbol: 'SPY', name: 'SPDR S&P 500', firstHeld: '2024-01-02', lastHeld: '2026-09-18' },
];

describe('ExperimentHistoryComponent', () => {
  let fixture: ComponentFixture<ExperimentHistoryComponent>;
  let getExperiments: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    getExperiments = vi.fn(() => of([]));
    await TestBed.configureTestingModule({
      imports: [ExperimentHistoryComponent],
      providers: [
        { provide: ResearchService, useValue: { getExperiments } },
        provideFakeTickerCatalog(fakeTickerCatalog(PICKER_POOL)),
        provideFakeVendorCatalog(fakeVendorCatalog()),
        provideFakeEnsureCoverage(fakeEnsureCoverage()),
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(ExperimentHistoryComponent);
  });

  function picker(): SymbolPickerComponent {
    const de = fixture.debugElement.query((node) => node.componentInstance instanceof SymbolPickerComponent);
    return de.componentInstance as SymbolPickerComponent;
  }

  it('picks its ticker through the shared picker — no free-text ticker input survives', () => {
    fixture.detectChanges();
    expect(fixture.debugElement.query((n) => n.componentInstance instanceof SymbolPickerComponent)).not.toBeNull();
    expect(fixture.nativeElement.querySelector('input[placeholder="Ticker"]')).toBeNull();
  });

  it('reloads the history the moment a symbol is picked', async () => {
    fixture.detectChanges();

    picker().symbol.set('SPY');
    await fixture.whenStable();

    expect(getExperiments).toHaveBeenCalledWith('SPY');
  });

  it('surfaces a failed history read', async () => {
    getExperiments.mockReturnValue(throwError(() => new Error('history endpoint down')));
    fixture.detectChanges();

    picker().symbol.set('SPY');
    await fixture.whenStable();
    fixture.detectChanges();

    expect((fixture.nativeElement.textContent ?? '')).toContain('history endpoint down');
  });
});
