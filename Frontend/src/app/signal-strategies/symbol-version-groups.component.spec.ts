import { signal } from '@angular/core';
import { render, screen, within } from '@testing-library/angular';
import { describe, expect, it } from 'vitest';

import { qualification } from './testing/fixtures';
import { SignalStrategyWorkspaceStore, type QualifiedVersions } from './signal-strategy-workspace.store';
import { versionsBySymbol } from './signal-strategy-evidence';
import { SymbolVersionGroupsComponent, VersionCellsDirective, VersionColumnsDirective } from './symbol-version-groups.component';

function read(versions: ReturnType<typeof qualification>[], mayBeCut = false): QualifiedVersions {
  return { bySymbol: versionsBySymbol('ema_crossover_signal', versions), mayBeCut };
}

async function show(versions: QualifiedVersions, goldenRefusal: string | null = null, template?: string) {
  return render(
    template ?? '<app-symbol-version-groups [versions]="versions" />',
    {
      imports: [SymbolVersionGroupsComponent, VersionCellsDirective, VersionColumnsDirective],
      componentProperties: { versions },
      providers: [{ provide: SignalStrategyWorkspaceStore, useValue: { goldenRefusal: signal(goldenRefusal).asReadonly() } }],
    },
  );
}

describe('SymbolVersionGroupsComponent', () => {
  it('shows each symbol’s versions with their status counts, status, exact settings and the default marked once', async () => {
    await show(read([
      qualification({ id: 'spy-ready', symbol: 'SPY', is_default: true, parameters: { slow_period: 12, fast_period: 6 } }),
      qualification({ id: 'spy-revoked', symbol: 'SPY', status: 'revoked', status_explanation: 'Revoked by the owner.', created_at_ms: 2 }),
      qualification({ id: 'qqq-bare', symbol: 'QQQ', parameters: {} }),
    ]));

    expect(screen.getAllByText(/1 ready/)).toHaveLength(2);
    expect(screen.getByText(/1 revoked/)).toBeTruthy();
    expect(screen.getAllByText('default for SPY')).toHaveLength(1);
    expect(screen.getByText('Revoked by the owner.')).toBeTruthy();
    expect(screen.getByText('fast_period = 6 · slow_period = 12')).toBeTruthy();
    expect(screen.getByText('no settings recorded')).toBeTruthy();
    expect(screen.queryByText(/at most/)).toBeNull();
  });

  it('puts a page’s own columns beside the shared ones, each cell given its version', async () => {
    await show(
      read([qualification({ id: 'spy-ready', symbol: 'SPY' }), qualification({ id: 'qqq-ready', symbol: 'QQQ', status: 'stale' })]),
      null,
      `<app-symbol-version-groups [versions]="versions">
         <ng-template appVersionColumns><th>Mine</th></ng-template>
         <ng-template appVersionCells let-version><td>{{ version.status === 'ready' ? 'use ' : 'cannot ' }}{{ version.id }}</td></ng-template>
       </app-symbol-version-groups>`,
    );

    expect(screen.getAllByRole('columnheader', { name: 'Mine' })).toHaveLength(2);
    expect(screen.getByRole('cell', { name: 'use spy-ready' })).toBeTruthy();
    expect(screen.getByRole('cell', { name: 'cannot qqq-ready' })).toBeTruthy();
  });

  it('says a read that may have been cut short may be, and gives the registry’s reason when it has no version to show', async () => {
    const { rerender } = await show(read([qualification({ symbol: 'SPY' })], true));
    expect(screen.getByText(/at most \d+ versions, so some may not be listed here/)).toBeTruthy();

    await rerender({ componentProperties: { versions: read([]) } });
    expect(screen.getByText(/^No symbol has a qualified version of this strategy yet\./)).toBeTruthy();
  });

  it('names the registry’s reason instead when Golden Search is ruled out', async () => {
    const view = await show(read([]), 'This strategy is an operational harness.');

    expect(within(view.container).getByText('This strategy is an operational harness.')).toBeTruthy();
    expect(screen.queryByText(/No symbol has a qualified version/)).toBeNull();
  });
});
