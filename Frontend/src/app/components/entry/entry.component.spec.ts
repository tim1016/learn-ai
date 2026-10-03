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

  it("opens Data Lab when I'm In is pressed, with no sign-in in between", async () => {
    await renderEntry();
    const router = TestBed.inject(Router);

    await userEvent.click(screen.getByRole('button', { name: "I'm In" }));

    await vi.waitFor(() => expect(router.url).toBe('/data-lab'));
  });

  it('opens Data Lab from the keyboard', async () => {
    await renderEntry();
    const router = TestBed.inject(Router);

    await userEvent.tab();
    await userEvent.keyboard('{Enter}');

    await vi.waitFor(() => expect(router.url).toBe('/data-lab'));
  });

  it('describes the painting to people who cannot see it', async () => {
    await renderEntry();

    expect(screen.getByRole('img', { name: /buy fear, sell greed/i })).toBeTruthy();
  });

  it('has no detectable accessibility violations', async () => {
    const { container } = await renderEntry();

    const results = await axe.run(container);

    expect(results.violations).toEqual([]);
  });
});
