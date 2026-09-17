import { provideHttpClient } from '@angular/common/http';
import { HttpTestingController, provideHttpClientTesting } from '@angular/common/http/testing';
import { TestBed } from '@angular/core/testing';
import { render, screen } from '@testing-library/angular';
import axe from 'axe-core';
import { describe, expect, it } from 'vitest';

import { ConfigurationHandoffScriptComponent, withWorkerExports } from './configuration-handoff-script.component';

const SCRIPT =
  '#!/usr/bin/env bash\n'
  + 'set -euo pipefail\n'
  + '\n'
  + 'worker_service="${FLEET_WORKER_SERVICE:?set it}"\n'
  + 'echo "handoff $worker_service"\n';

async function renderScript(restartCommand: string | null | undefined) {
  const view = await render(ConfigurationHandoffScriptComponent, {
    inputs: { restartCommand },
    providers: [provideHttpClient(), provideHttpClientTesting()],
  });
  TestBed.inject(HttpTestingController)
    .expectOne('/assets/scripts/alpaca-paper-to-live-handoff.sh')
    .flush(SCRIPT);
  await view.fixture.whenStable();
  return view;
}

describe('ConfigurationHandoffScriptComponent', () => {
  it('shows the executable asset with this lane worker service spliced in', async () => {
    await renderScript('podman compose restart alpaca-paper-clerk');

    expect(screen.getByRole('button', { name: 'Copy Paper-to-Live handoff script' })).toBeTruthy();
    // #2183: "Paper → Live handoff script" carries the eyebrow look itself
    // now; the separate "Local operator tool" label above it is retired.
    expect(screen.getByRole('heading', { name: 'Paper → Live handoff script' })).toBeTruthy();
    expect(screen.queryByText('Local operator tool')).toBeNull();
    expect(document.querySelector('.handoff__source code')?.textContent).toBe(
      '#!/usr/bin/env bash\n'
      + 'set -euo pipefail\n'
      + '\nexport FLEET_WORKER_SERVICE=\'alpaca-paper-clerk\'\n'
      + '\n'
      + 'worker_service="${FLEET_WORKER_SERVICE:?set it}"\n'
      + 'echo "handoff $worker_service"\n',
    );
    expect(screen.getByText(/never arms or launches a Live bot/)).toBeTruthy();
  });

  it('splices in the full compose context, in the same order the desk authored it', async () => {
    await renderScript(
      'podman compose --project-name learn-ai-fleet -f compose.yaml -f compose.fleet.yaml '
      + '--profile fleet restart alpaca-live-clerk',
    );

    const source = document.querySelector('.handoff__source code')?.textContent ?? '';
    const exportBlock = source.split('worker_service=')[0];
    expect(exportBlock).toContain('export FLEET_WORKER_SERVICE=\'alpaca-live-clerk\'\n');
    expect(exportBlock).toContain('export FLEET_COMPOSE_PROJECT=\'learn-ai-fleet\'\n');
    expect(exportBlock).toContain('export FLEET_COMPOSE_FILES=\'compose.yaml,compose.fleet.yaml\'\n');
    expect(exportBlock).toContain('export FLEET_COMPOSE_PROFILE=\'fleet\'\n');
  });

  it('leaves the asset unchanged, with a warning, when the deployment declared no worker service', async () => {
    await renderScript(null);

    expect(document.querySelector('.handoff__source code')?.textContent).toBe(SCRIPT);
    expect(screen.getByText(/did not declare this lane's worker service/)).toBeTruthy();
  });

  it('leaves the asset unchanged and says nothing while the desk read has not landed', async () => {
    await renderScript(undefined);

    expect(document.querySelector('.handoff__source code')?.textContent).toBe(SCRIPT);
    expect(screen.queryByText(/did not declare this lane's worker service/)).toBeNull();
  });

  it('has no detectable accessibility violations', async () => {
    await renderScript('podman compose restart alpaca-paper-clerk');

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });

  it('has no detectable accessibility violations when no worker service was declared', async () => {
    await renderScript(null);

    const results = await axe.run(document.body, {
      rules: { 'color-contrast': { enabled: false } },
    });

    expect(results.violations).toEqual([]);
  });
});

describe('withWorkerExports', () => {
  it('never splices a value that carries an embedded single quote', () => {
    // A compose identifier cannot legitimately need one; a value that has
    // one is a shape this component refuses to guess a safe substitution
    // for, rather than emit bash that breaks out of its own quoting.
    expect(withWorkerExports(SCRIPT, "podman compose restart alpaca-paper-clerk's-clone")).toBe(SCRIPT);
  });

  it('leaves the script untouched when the command does not match the known grammar', () => {
    expect(withWorkerExports(SCRIPT, 'echo not-a-compose-command')).toBe(SCRIPT);
  });

  it('leaves the script untouched when the anchor line is missing', () => {
    const scriptWithoutAnchor = '#!/usr/bin/env bash\necho hi\n';
    expect(withWorkerExports(scriptWithoutAnchor, 'podman compose restart alpaca-paper-clerk')).toBe(
      scriptWithoutAnchor,
    );
  });
});
