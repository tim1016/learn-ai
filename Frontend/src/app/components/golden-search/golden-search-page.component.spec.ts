import { provideRouter, Router } from '@angular/router';
import { render, screen, waitFor } from '@testing-library/angular';
import { describe, expect, it, vi } from 'vitest';

import { fakePickerWorld } from '../../shared/symbol-picker/testing/fake-picker-world';
import { GoldenSearchPageComponent } from './golden-search-page.component';
import { GoldenSearchService } from './golden-search.service';
import { defaults, emaCapability, preflight, studyDetail } from './testing/fixtures';

function fakeService() {
  return {
    capabilities: vi.fn(async () => [emaCapability()]),
    defaults: vi.fn(async () => defaults()),
    preflight: vi.fn(async () => preflight()),
    list: vi.fn(async () => []),
    get: vi.fn(async (_id: string) => studyDetail('awaiting_validation')),
  };
}

async function renderPage(inputs: { studyId?: string; revise?: string }, service = fakeService()) {
  const view = await render(GoldenSearchPageComponent, {
    inputs,
    providers: [provideRouter([]), ...fakePickerWorld().providers, { provide: GoldenSearchService, useValue: service }],
  });
  return { view, service };
}

describe('GoldenSearchPageComponent', () => {
  it('opens one study’s workbench when the route names it', async () => {
    const { service } = await renderPage({ studyId: 'study-0001-aaaa' });

    expect(await screen.findByRole('navigation', { name: 'Research steps' })).not.toBeNull();
    expect(service.get).toHaveBeenCalledWith('study-0001-aaaa');
    expect(screen.queryByRole('tab', { name: /new study/i })).toBeNull();
  });

  it('starts a revision from the named study’s frozen plan', async () => {
    const { service } = await renderPage({ revise: 'study-0001-aaaa' });

    expect(await screen.findByText(/Revising study study-00 as a new study/)).not.toBeNull();
    expect(screen.getByRole('button', { name: /lock as a new study/i })).not.toBeNull();
    expect(service.get).toHaveBeenCalledWith('study-0001-aaaa');
    expect(service.defaults).not.toHaveBeenCalled();
  });

  it('goes to the new study once a plan is locked', async () => {
    const { view } = await renderPage({});
    const navigate = vi.spyOn(view.fixture.debugElement.injector.get(Router), 'navigate').mockResolvedValue(true);

    view.fixture.componentInstance.onLocked('study-0009-zzzz');

    await waitFor(() => expect(navigate).toHaveBeenCalledWith(['/golden-search', 'study-0009-zzzz']));
  });
});
