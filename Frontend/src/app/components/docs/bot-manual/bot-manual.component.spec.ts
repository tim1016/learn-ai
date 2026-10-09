import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { provideRouter } from '@angular/router';
import { render, screen } from '@testing-library/angular';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { BotManualComponent } from './bot-manual.component';

describe('BotManualComponent', () => {
  afterEach(() => vi.restoreAllMocks());

  it('allows printing only after the complete document has loaded', async () => {
    const print = vi.spyOn(window, 'print').mockImplementation(() => undefined);
    const fixture = await render(BotManualComponent, {
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });
    const button = screen.getByRole('button', { name: 'Print / Save PDF' });
    expect(button.hasAttribute('disabled')).toBe(true);
    await userEvent.click(button);
    expect(print).not.toHaveBeenCalled();

    TestBed.inject(HttpTestingController).expectOne('/assets/docs/bot-business-manual.md')
      .flush('## First chapter\n\nFull text.\n\n## Last chapter\n\nFinal text.');
    fixture.detectChanges();
    await userEvent.click(button);
    expect(print).toHaveBeenCalledOnce();
    expect(screen.getByRole('heading', { name: 'Last chapter' })).toBeTruthy();
  });

  it('keeps printing unavailable when the document fails to load', async () => {
    const fixture = await render(BotManualComponent, {
      providers: [provideRouter([]), provideHttpClient(), provideHttpClientTesting()],
    });
    TestBed.inject(HttpTestingController).expectOne('/assets/docs/bot-business-manual.md')
      .flush('Unavailable', { status: 503, statusText: 'Service Unavailable' });
    fixture.detectChanges();
    expect(screen.getByRole('button', { name: 'Print / Save PDF' }).hasAttribute('disabled')).toBe(true);
    expect(screen.getByText(/Failed to load/)).toBeTruthy();
  });
});
