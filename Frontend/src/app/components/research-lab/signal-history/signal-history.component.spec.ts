import { ComponentFixture, TestBed } from '@angular/core/testing';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { of, throwError } from 'rxjs';

import { ResearchService } from '../../../services/research.service';
import { SignalHistoryComponent } from './signal-history.component';
import {
  fakePickerWorld,
  pickSymbol,
  symbolPicker,
} from '../../../shared/symbol-picker/testing/fake-picker-world';

describe('SignalHistoryComponent', () => {
  let fixture: ComponentFixture<SignalHistoryComponent>;
  let getSignalExperiments: ReturnType<typeof vi.fn>;

  beforeEach(async () => {
    TestBed.resetTestingModule();
    getSignalExperiments = vi.fn(() => of([]));
    await TestBed.configureTestingModule({
      imports: [SignalHistoryComponent],
      providers: [
        { provide: ResearchService, useValue: { getSignalExperiments } },
        ...fakePickerWorld().providers,
      ],
    }).compileComponents();

    fixture = TestBed.createComponent(SignalHistoryComponent);
  });

  it('picks its ticker through the shared picker — no free-text ticker input survives', () => {
    fixture.detectChanges();
    expect(() => symbolPicker(fixture)).not.toThrow();
    expect(fixture.nativeElement.querySelector('input[placeholder="Ticker"]')).toBeNull();
  });

  it('reloads the history the moment a symbol is picked', async () => {
    fixture.detectChanges();

    pickSymbol(fixture, 'SPY');
    await fixture.whenStable();

    expect(getSignalExperiments).toHaveBeenCalledWith('SPY');
  });

  it('surfaces a failed history read', async () => {
    getSignalExperiments.mockReturnValue(throwError(() => new Error('history endpoint down')));
    fixture.detectChanges();

    pickSymbol(fixture, 'SPY');
    await fixture.whenStable();
    fixture.detectChanges();

    expect((fixture.nativeElement.textContent ?? '')).toContain('history endpoint down');
  });
});
