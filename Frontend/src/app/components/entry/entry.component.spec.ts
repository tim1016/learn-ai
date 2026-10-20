import { ChangeDetectionStrategy, Component } from '@angular/core';
import { TestBed } from '@angular/core/testing';
import { Router, provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import axe from 'axe-core';
import { describe, expect, it, vi } from 'vitest';

import { EntryComponent } from './entry.component';

@Component({ template: '<p>Data Lab</p>', changeDetection: ChangeDetectionStrategy.OnPush })
class DataLabStubComponent {}

// jsdom has no WebGL and never loads pictures, so these run the page the way a
// browser without WebGL shows it: the plain painting with the button over it.
describe('EntryComponent', () => {
  const renderEntry = () =>
    render(EntryComponent, {
      providers: [provideRouter([{ path: 'data-lab', component: DataLabStubComponent }])],
    });

  it.each<[string, () => Promise<void>]>([
    [
      "I'm In is pressed, with no sign-in in between",
      () => userEvent.click(screen.getByRole('button', { name: "I'm In" })),
    ],
    [
      'the keyboard is used',
      async () => {
        await userEvent.tab();
        await userEvent.keyboard('{Enter}');
      },
    ],
  ])('opens Data Lab when %s', async (_name, act) => {
    await renderEntry();
    const router = TestBed.inject(Router);

    await act();

    await vi.waitFor(() => expect(router.url).toBe('/data-lab'));
  });

  it('describes the painting to people who cannot see it, and has no detectable accessibility violations', async () => {
    const { container } = await renderEntry();

    expect
      .soft(screen.queryByRole('img', { name: /buy fear, sell greed/i }), 'describes the painting to people who cannot see it')
      .toBeTruthy();

    const results = await axe.run(container);

    expect.soft(results.violations, 'has no detectable accessibility violations').toEqual([]);
  });
});
