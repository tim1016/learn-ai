import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { screen } from '@testing-library/dom';
import { beforeEach, describe, expect, it } from 'vitest';

import { SpyEmaWalkForwardManualPageComponent } from './spy-ema-walk-forward-manual-page.component';

describe('SpyEmaWalkForwardManualPageComponent', () => {
  beforeEach(() => {
    TestBed.configureTestingModule({
      imports: [SpyEmaWalkForwardManualPageComponent],
      providers: [
        provideRouter([]),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
  });

  it('presents the field manual and its research-to-execution reading path', () => {
    const fixture = TestBed.createComponent(SpyEmaWalkForwardManualPageComponent);
    fixture.detectChanges();

    expect(screen.getByRole('heading', {
      name: 'SPY EMA walk-forward: from research to execution',
    })).toBeTruthy();
    expect(screen.getByRole('navigation', { name: 'User manual sections' })).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Future execution path' })).toBeTruthy();
    expect(screen.getByText(/validate the rule that chooses the threshold/i)).toBeTruthy();
  });

  it('loads the versioned manual asset and links back to the study', () => {
    const fixture = TestBed.createComponent(SpyEmaWalkForwardManualPageComponent);
    fixture.detectChanges();

    const viewer = fixture.nativeElement.querySelector('app-markdown-viewer') as HTMLElement | null;
    const rawLink = screen.getByRole('link', { name: /raw markdown/i });
    const studyLink = screen.getByRole('link', { name: /back to study/i });

    expect(viewer).toBeTruthy();
    expect(fixture.componentInstance.src).toBe('/assets/docs/spy-ema-walk-forward-user-manual.md');
    expect(rawLink.getAttribute('href')).toBe('/assets/docs/spy-ema-walk-forward-user-manual.md');
    expect(studyLink.getAttribute('href')).toBe('/research-lab/backtests/spy-ema-walk-forward');
  });
});
