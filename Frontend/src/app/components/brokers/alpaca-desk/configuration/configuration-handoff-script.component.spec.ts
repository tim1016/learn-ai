import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { ConfigurationHandoffScriptComponent } from './configuration-handoff-script.component';

const SCRIPT = '#!/usr/bin/env bash\necho "handoff"\n';

async function renderScript() {
  const view = await render(ConfigurationHandoffScriptComponent, {
    providers: [provideHttpClient(), provideHttpClientTesting()],
  });
  TestBed.inject(HttpTestingController)
    .expectOne('/assets/scripts/alpaca-paper-to-live-handoff.sh')
    .flush(SCRIPT);
  await view.fixture.whenStable();
  return view;
}

describe('ConfigurationHandoffScriptComponent', () => {
  it('shows the exact executable asset with an accessible copy action', async () => {
    await renderScript();

    expect(screen.getByRole('button', { name: 'Copy Paper-to-Live handoff script' })).toBeTruthy();
    expect(document.querySelector('.handoff__source code')?.textContent).toBe(SCRIPT);
    expect(screen.getByText(/never arms or launches a Live bot/)).toBeTruthy();
  });

  it('has no detectable accessibility violations', async () => {
    await renderScript();

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});
