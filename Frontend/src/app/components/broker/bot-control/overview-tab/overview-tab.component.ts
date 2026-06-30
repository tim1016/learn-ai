import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';

import type {
  LifecycleChartEdge,
  LifecycleChartNode,
  LifecycleChartStatus,
  LiveInstanceStatus,
} from '../../../../api/live-instances.types';

interface ExpandedGraphSelection {
  readonly chartKey: string;
  readonly graphId: string;
}

interface LifecycleFlowRow {
  readonly node: LifecycleChartNode;
  readonly outgoingEdges: readonly LifecycleChartEdge[];
}

@Component({
  selector: 'app-overview-tab',
  imports: [CommonModule],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './overview-tab.component.html',
  styleUrl: './overview-tab.component.scss',
})
export class OverviewTabComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly selectedNodeId = input<string | null>(null);
  readonly highlightedNodeId = input<string | null>(null);
  readonly nodeSelected = output<LifecycleChartNode>();

  readonly expandedGraphSelection = signal<ExpandedGraphSelection | null>(null);
  readonly chart = computed(() => this.status().lifecycle_chart);
  readonly chartKey = computed(() => {
    const status = this.status();
    const chart = status.lifecycle_chart;
    return `${status.strategy_instance_id}:${chart.chart_id}:${chart.selected_bot_id}`;
  });
  readonly currentGraph = computed(() => {
    const chart = this.chart();
    const expanded = this.expandedGraphSelection();
    if (expanded?.chartKey === this.chartKey() && chart.subgraphs[expanded.graphId]) {
      return chart.subgraphs[expanded.graphId];
    }
    return chart.global_graph;
  });
  readonly isExpanded = computed(() => this.currentGraph().graph_id !== 'global');
  readonly activeNode = computed(() => {
    const graph = this.currentGraph();
    return graph.nodes.find((node) => node.id === graph.primary_node_id) ?? null;
  });
  readonly lifecycleNodes = computed(() => this.currentGraph().nodes);
  readonly lifecycleEdges = computed(() => this.currentGraph().edges);
  readonly lifecycleFlowRows = computed<readonly LifecycleFlowRow[]>(() => {
    const edgesBySource = new Map<string, LifecycleChartEdge[]>();
    for (const edge of this.lifecycleEdges()) {
      const edges = edgesBySource.get(edge.source);
      if (edges) {
        edges.push(edge);
      } else {
        edgesBySource.set(edge.source, [edge]);
      }
    }
    return this.lifecycleNodes().map((node) => ({
      node,
      outgoingEdges: edgesBySource.get(node.id) ?? [],
    }));
  });
  readonly nodeLabels = computed(() => {
    const graph = this.currentGraph();
    return new Map(graph.nodes.map((node) => [node.id, node.label]));
  });

  collapse(): void {
    this.expandedGraphSelection.set(null);
  }

  expandNode(node: LifecycleChartNode): void {
    this.nodeSelected.emit(node);
    if (!node.expandable || !node.subgraph_id || !this.chart().subgraphs[node.subgraph_id]) return;
    this.expandedGraphSelection.set({
      chartKey: this.chartKey(),
      graphId: node.subgraph_id,
    });
  }

  onNodeKeydown(event: KeyboardEvent, node: LifecycleChartNode): void {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    this.expandNode(node);
  }

  nodeAriaLabel(node: LifecycleChartNode): string {
    const action = node.expandable ? 'Open' : 'Select';
    const callout = this.isBlockingNode(node)
      ? ' Blocking step.'
      : this.isPrimaryNode(node)
        ? ' Current step.'
        : '';
    return `${action} ${node.label}. Status: ${node.status_label}.${callout}`;
  }

  isPrimaryNode(node: LifecycleChartNode): boolean {
    return node.id === this.currentGraph().primary_node_id;
  }

  isBlockingNode(node: LifecycleChartNode): boolean {
    return this.isPrimaryNode(node) && this.isBlockingStatus(node.status);
  }

  isBlockingStatus(status: LifecycleChartStatus): boolean {
    return status === 'blocked' || status === 'poison' || status === 'freeze' || status === 'unknown';
  }

  visualEdgeStatus(sourceNode: LifecycleChartNode, edge: LifecycleChartEdge): LifecycleChartStatus {
    if (this.isBlockingNode(sourceNode) && (edge.status === 'inactive' || edge.status === 'unknown')) {
      return sourceNode.status;
    }
    return edge.status;
  }

  edgeColor(status: LifecycleChartStatus): string {
    switch (status) {
      case 'passed':
        return 'var(--bull)';
      case 'active':
        return 'var(--accent)';
      case 'blocked':
        return 'var(--warn)';
      case 'poison':
        return 'var(--bear)';
      case 'freeze':
        return 'var(--info)';
      case 'unknown':
        return 'var(--text-muted)';
      case 'inactive':
        return 'var(--border-light)';
    }
  }

  edgeEndpointLabel(nodeId: string): string {
    return this.nodeLabels().get(nodeId) ?? nodeId;
  }

  edgeStatusLabel(edge: LifecycleChartEdge): string {
    return this.lifecycleStatusLabel(edge.status);
  }

  lifecycleStatusLabel(status: LifecycleChartStatus): string {
    switch (status) {
      case 'passed':
        return 'Passed';
      case 'active':
        return 'Active';
      case 'blocked':
        return 'Blocked';
      case 'poison':
        return 'Poison';
      case 'freeze':
        return 'Freeze';
      case 'unknown':
        return 'Unknown';
      case 'inactive':
        return 'Waiting';
    }
  }
}
