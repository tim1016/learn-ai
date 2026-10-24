import { render, screen } from '@testing-library/angular';
import { Router, provideRouter } from '@angular/router';
import { TestBed } from '@angular/core/testing';
import { describe, expect, it } from 'vitest';

import { HOME_ROUTE, NOT_FOUND_TITLE } from './app-menu';
import { NotFoundComponent } from './not-found.component';

describe('NotFoundComponent', () => {
  it('says no page is at the address, without the query or fragment, and leads back to Markets', async () => {
    const view = await render(NotFoundComponent, {
      providers: [provideRouter([{ path: '**', component: NotFoundComponent }])],
    });
    await TestBed.inject(Router).navigateByUrl('/research-lab/nothing?x=1#frag');
    view.fixture.detectChanges();

    expect(screen.getByRole('heading', { name: NOT_FOUND_TITLE })).toBeTruthy();
    expect(await screen.findByText('No page is at /research-lab/nothing.')).toBeTruthy();
    expect(screen.getByRole('link', { name: 'Go to Markets' }).getAttribute('href')).toBe(HOME_ROUTE);
  });
});
