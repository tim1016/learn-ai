import { TestBed } from '@angular/core/testing';
import { afterEach, describe, expect, it } from 'vitest';

import type { LifecycleChartNode } from '../../../../api/live-instances.types';
import { makeLifecycleChartFixture } from '../../../../testing/live-instance-status-fixtures';
import { LifecycleNodeCardComponent } from './lifecycle-node-card.component';

function deployNode(): LifecycleChartNode {
  const node = makeLifecycleChartFixture().global_graph.nodes[0];
  return {
    ...node,
    receipts: [
      {
        label: 'deploy.state',
        value: 'READY',
        headline: 'Deploy gate is ready.',
        detail: 'The host start gate is ready.',
        unit: null,
        source: 'operator_surface',
        gate_id: 'desired_state.start',
        ts_ms: 1_700_000_000_000,
        ts_ms_resolved: true,
      },
    ],
  };
}

function render(node: LifecycleChartNode, receiptsExpanded = false) {
  const fixture = TestBed.createComponent(LifecycleNodeCardComponent);
  fixture.componentRef.setInput('node', node);
  fixture.componentRef.setInput('headingId', `heading-${node.id}`);
  fixture.componentRef.setInput('receiptRegionId', `receipts-${node.id}`);
  fixture.componentRef.setInput('primary', true);
  fixture.componentRef.setInput('receiptsExpanded', receiptsExpanded);
  fixture.detectChanges();
  return fixture;
}

afterEach(() => TestBed.resetTestingModule());

describe('LifecycleNodeCardComponent', () => {
  it('keeps the card non-interactive and exposes primary/select and receipts buttons', () => {
    const fixture = render(deployNode());
    const el = fixture.nativeElement as HTMLElement;
    const selectedIds: string[] = [];
    const toggledIds: string[] = [];
    fixture.componentInstance.selectedRequested.subscribe((node) => selectedIds.push(node.id));
    fixture.componentInstance.receiptsToggled.subscribe((node) => toggledIds.push(node.id));

    const card = el.querySelector<HTMLElement>('.flow-node');
    expect(card?.getAttribute('role')).toBeNull();

    el.querySelector<HTMLButtonElement>('[aria-label^="Select Deploy or start"]')?.click();
    el.querySelector<HTMLButtonElement>('[aria-controls="receipts-deploy"]')?.click();

    expect(selectedIds).toEqual(['deploy']);
    expect(toggledIds).toEqual(['deploy']);
  });

  it('emits subgraph open through a contextual button', () => {
    const fixture = render(deployNode());
    const el = fixture.nativeElement as HTMLElement;
    const openedIds: string[] = [];
    fixture.componentInstance.subgraphRequested.subscribe((node) => openedIds.push(node.id));

    el.querySelector<HTMLButtonElement>('[aria-label^="Open Deploy or start details"]')?.click();

    expect(openedIds).toEqual(['deploy']);
  });

  it('shows receipt rows directly inside the expanded region', () => {
    const fixture = render(deployNode(), true);
    const el = fixture.nativeElement as HTMLElement;

    const region = el.querySelector<HTMLElement>('[data-testid="lifecycle-node-receipts-deploy"]');
    expect(region?.textContent).toContain('Deploy gate is ready.');
    expect(region?.textContent).toContain('The host start gate is ready.');
    expect(region?.querySelector(':scope > app-node-receipts-list')).not.toBeNull();
    expect(region?.querySelector(':scope > details')).toBeNull();
  });
});
