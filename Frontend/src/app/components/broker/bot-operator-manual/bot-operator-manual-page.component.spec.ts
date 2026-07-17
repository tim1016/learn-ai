import { provideHttpClient } from '@angular/common/http';
import { provideHttpClientTesting, HttpTestingController } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { fireEvent, render, screen } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { BotOperatorManualPageComponent } from './bot-operator-manual-page.component';

describe('BotOperatorManualPageComponent', () => {
  it('renders canonical chapter text and links the manual map directly to it', async () => {
    await render(BotOperatorManualPageComponent, {
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });

    const http = TestBed.inject(HttpTestingController);
    http.expectOne('/assets/docs/bot-control-operator-manual.md').flush(
      '# Bot Control & Account Clerk — Operator Manual\n\n## 1. Mental model — three planes\n\nEverything in this system is one of three planes.',
    );

    expect(screen.getByRole('heading', { name: 'Operate the bot. Protect the account.' })).toBeTruthy();
    expect(screen.getByText('Create and start a bot')).toBeTruthy();
    expect(screen.getByText('Stop a bot')).toBeTruthy();
    expect(await screen.findByText('Everything in this system is one of three planes.')).toBeTruthy();
    const chapterLink = screen.getByRole('link', { name: /Know the system/ });
    expect(chapterLink.getAttribute('href')).toBe('/broker/bot-manual#1-mental-model-three-planes');
    expect(screen.getByText('Bot Control & Account Clerk — Operator Manual')).toBeTruthy();
    expect(screen.getByRole('navigation', { name: 'Manual contents' })).toBeTruthy();
    http.verify();
  });

  it('keeps the unfiltered Markdown source hidden until requested', async () => {
    await render(BotOperatorManualPageComponent, {
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });

    const http = TestBed.inject(HttpTestingController);
    http.expectOne('/assets/docs/bot-control-operator-manual.md').flush(
      '# Bot Control & Account Clerk — Operator Manual\n\n## 1. Mental model — three planes\n\nCanonical operator content.',
    );

    expect(screen.queryByRole('heading', { name: 'Bot Control & Account Clerk — Operator Manual' })).toBeNull();

    const sourceReference = screen.getByText('Full source Markdown').closest('details');
    if (sourceReference === null) throw new Error('Expected the source reference disclosure.');
    sourceReference.open = true;
    fireEvent(sourceReference, new Event('toggle'));
    http.expectOne('/assets/docs/bot-control-operator-manual.md').flush(
      '# Bot Control & Account Clerk — Operator Manual\n\nCanonical operator content.',
    );

    expect(await screen.findByRole('heading', { name: 'Bot Control & Account Clerk — Operator Manual' })).toBeTruthy();
    http.verify();
  });
});
