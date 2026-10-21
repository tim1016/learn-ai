import { ComponentFixture, TestBed } from '@angular/core/testing';
import { ActivatedRoute, convertToParamMap } from '@angular/router';
import { describe, it, expect, vi } from 'vitest';
import { of, throwError } from 'rxjs';

import { ResearchService } from '../../../services/research.service';
import { SignalReportPageComponent } from './signal-report-page.component';

async function renderPage(id: string, getSignalExperimentReport: ReturnType<typeof vi.fn>): Promise<ComponentFixture<SignalReportPageComponent>> {
  TestBed.resetTestingModule();
  await TestBed.configureTestingModule({
    imports: [SignalReportPageComponent],
    providers: [
      { provide: ResearchService, useValue: { getSignalExperimentReport } },
      { provide: ActivatedRoute, useValue: { snapshot: { paramMap: convertToParamMap({ id }) } } },
    ],
  }).compileComponents();
  const fixture = TestBed.createComponent(SignalReportPageComponent);
  fixture.detectChanges();
  await fixture.whenStable();
  fixture.detectChanges();
  return fixture;
}

describe('SignalReportPageComponent', () => {
  it('says the experiment was not found when no report is held for it', async () => {
    const fixture = await renderPage('999', vi.fn(() => of(null)));

    expect(fixture.nativeElement.textContent).toContain('Signal experiment not found');
  });

  it('shows the sentence of a report that could not be read', async () => {
    const fixture = await renderPage('42', vi.fn(() => throwError(
      () => new Error('Could not load the signal report: The stored report of signal experiment 42 cannot be read: bad JSON'),
    )));

    expect(fixture.nativeElement.textContent).toContain('The stored report of signal experiment 42 cannot be read: bad JSON');
  });
});
