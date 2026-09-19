import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter, withComponentInputBinding } from '@angular/router';
import { RouterTestingHarness } from '@angular/router/testing';
import { render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { routes } from '../../app.routes';
import { MarkdownDocPageComponent } from './markdown-doc-page.component';

describe('MarkdownDocPageComponent', () => {
  it('titles the page from its route data and renders that document', async () => {
    const fixture = await render(MarkdownDocPageComponent, {
      inputs: { heading: 'Architecture Manual', src: '/assets/docs/architecture-manual.md' },
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });

    TestBed.inject(HttpTestingController)
      .expectOne('/assets/docs/architecture-manual.md')
      .flush('## The one-picture map\n');
    fixture.detectChanges();

    expect(screen.getByRole('heading', { level: 1, name: 'Architecture Manual' })).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: 'The one-picture map' })).toBeTruthy();
    expect(screen.getByRole('link', { name: /Raw markdown/ }).getAttribute('href')).toBe(
      '/assets/docs/architecture-manual.md',
    );
  });

  it('receives its heading and document from the real route table', async () => {
    TestBed.configureTestingModule({
      providers: [
        provideRouter(routes, withComponentInputBinding()),
        provideHttpClient(),
        provideHttpClientTesting(),
      ],
    });
    const harness = await RouterTestingHarness.create();

    await harness.navigateByUrl('/docs/architecture-manual');
    TestBed.inject(HttpTestingController)
      .expectOne('/assets/docs/architecture-manual.md')
      .flush('## The one-picture map\n');
    harness.detectChanges();

    expect(harness.routeNativeElement?.querySelector('h1')?.textContent).toContain(
      'Architecture Manual',
    );
  });
});
