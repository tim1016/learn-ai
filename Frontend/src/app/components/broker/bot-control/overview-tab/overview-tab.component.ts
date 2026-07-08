import { ChangeDetectionStrategy, Component, computed, input, output, signal } from '@angular/core';

import type {
  LifecycleChartEdge,
  LifecycleChartNode,
  LifecycleChartStatus,
  LiveInstanceStatus,
  OperatorSurfaceAttentionGroup,
  OperatorSurfaceBlockageStage,
} from '../../../../api/live-instances.types';
import { LifecycleNodeCardComponent } from './lifecycle-node-card.component';

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
  imports: [LifecycleNodeCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './overview-tab.component.html',
  styleUrl: './overview-tab.component.scss',
})
export class OverviewTabComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly selectedNodeId = input<string | null>(null);
  readonly highlightedNodeId = input<string | null>(null);
  readonly recoveryOverrideBusy = input(false);
  readonly nodeSelected = output<LifecycleChartNode>();
  readonly crashRecoveryOverrideRequested = output();

  readonly expandedGraphSelection = signal<ExpandedGraphSelection | null>(null);
  readonly expandedReceiptNodeId = signal<string | null>(null);
  readonly flowCollapsed = signal(false);
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
  readonly blockageLadder = computed(() => this.status().operator_surface.blockage_ladder);
  readonly currentBlockage = computed<OperatorSurfaceBlockageStage | null>(() => {
    const ladder = this.blockageLadder();
    return ladder.stages.find((stage) => stage.current) ?? null;
  });
  readonly crashRecoveryRequired = computed(
    () => this.status().operator_surface.host_process.start_capability.disabled_reason_code === 'CRASH_RECOVERY_REQUIRED',
  );
  readonly attentionGroups = computed<readonly OperatorSurfaceAttentionGroup[]>(
    () => this.status().operator_surface.trader_guidance?.additional_attention_groups ?? [],
  );
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

  toggleFlowCollapsed(): void {
    this.flowCollapsed.update((collapsed) => !collapsed);
  }

  expandNode(node: LifecycleChartNode): void {
    this.nodeSelected.emit(node);
    if (!node.expandable || !node.subgraph_id || !this.chart().subgraphs[node.subgraph_id]) return;
    this.expandedReceiptNodeId.set(null);
    this.expandedGraphSelection.set({
      chartKey: this.chartKey(),
      graphId: node.subgraph_id,
    });
  }

  toggleNodeReceipts(node: LifecycleChartNode): void {
    this.nodeSelected.emit(node);
    const key = this.nodeReceiptKey(node);
    this.expandedReceiptNodeId.update((current) => current === key ? null : key);
  }

  isNodeReceiptsExpanded(node: LifecycleChartNode): boolean {
    return this.expandedReceiptNodeId() === this.nodeReceiptKey(node);
  }

  nodeReceiptRegionId(node: LifecycleChartNode): string {
    return `lifecycle-node-receipts-${this.currentGraph().graph_id}-${node.id}`;
  }

  nodeHeadingId(node: LifecycleChartNode): string {
    return `lifecycle-node-heading-${this.currentGraph().graph_id}-${node.id}`;
  }

  nodeReceiptKey(node: LifecycleChartNode): string {
    return `${this.chartKey()}:${this.currentGraph().graph_id}:${node.id}`;
  }

  selectNode(node: LifecycleChartNode): void {
    this.nodeSelected.emit(node);
  }

  recordCrashRecoveryOverride(): void {
    if (this.recoveryOverrideBusy()) return;
    this.crashRecoveryOverrideRequested.emit();
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

  blockageStageAria(stage: OperatorSurfaceBlockageStage): string {
    const current = stage.current ? ' Current signal.' : '';
    return `${stage.label}. ${stage.title}. ${stage.summary}${current}`;
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
