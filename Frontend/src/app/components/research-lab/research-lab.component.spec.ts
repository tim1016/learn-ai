import { Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { ResearchLabComponent } from './research-lab.component';
import { RESEARCH_LAB_NAV } from './research-lab-nav.config';

@Component({ template: '' })
class StudyStubComponent {}

async function openStudy(path: string): Promise<void> {
  TestBed.configureTestingModule({
    providers: [
      provideRouter([
        {
          path: '',
          component: ResearchLabComponent,
          children: RESEARCH_LAB_NAV.flatMap((group) => group.items).map((item) => ({
            path: item.path,
            component: StudyStubComponent,
            data: { title: item.label },
          })),
        },
      ]),
    ],
  });
  const harness = await RouterTestingHarness.create();
  await harness.navigateByUrl(`/${path}`);
}

describe('ResearchLabComponent help', () => {
  it.each([
    ['features/validate', '/data-lab-docs'],
    ['signals/engine', '/docs/signal-engine-methodology'],
    ['backtests/reliability', '/docs/indicator-reliability-methodology#1-context-and-scope'],
  ])('opens the document that explains the %s study in a new tab, so the study keeps its inputs', async (path, address) => {
    await openStudy(path);

    const link = screen.getByRole('link', { name: 'How this works' });
    expect(link.getAttribute('href')).toBe(address);
    expect(link.getAttribute('target')).toBe('_blank');
  });

  it('offers no link on a study that has no document', async () => {
    await openStudy('features/experiments');

    expect(screen.queryByRole('link', { name: 'How this works' })).toBeNull();
  });
});
