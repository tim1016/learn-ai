import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output, signal, type Signal } from '@angular/core';
import { createEdges, createNodes, type Edge, type Node, Vflow } from 'ngx-vflow';

import type {
  LifecycleChartAction,
  LifecycleChartActionId,
  LifecycleChartEdge,
  LifecycleChartGraph,
  LifecycleChartNode,
  LifecycleChartStatus,
  LiveInstanceStatus,
} from '../../../../api/live-instances.types';

interface Point {
  readonly x: number;
  readonly y: number;
}

interface VflowDataContext<T> {
  readonly data: Signal<T>;
}

const NODE_WIDTH = 190;
const NODE_HEIGHT = 96;

const GLOBAL_LAYOUT: Record<string, Point> = {
  deploy: { x: 20, y: 112 },
  preflight: { x: 270, y: 112 },
  account_safety: { x: 520, y: 28 },
  reconcile: { x: 770, y: 112 },
  activate: { x: 1020, y: 112 },
  active: { x: 1270, y: 112 },
  submit_order: { x: 1520, y: 28 },
  broker_writer: { x: 1770, y: 28 },
  recovery: { x: 1520, y: 196 },
};

@Component({
  selector: 'app-overview-tab',
  imports: [CommonModule, Vflow],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './overview-tab.component.html',
  styleUrl: './overview-tab.component.scss',
})
export class OverviewTabComponent {
  readonly status = input.required<LiveInstanceStatus>();
  readonly busyAction = input<string | null>(null);
  readonly actionInvoked = output<LifecycleChartActionId>();

  readonly expandedGraphId = signal<string | null>(null);
  readonly chart = computed(() => this.status().lifecycle_chart);
  readonly currentGraph = computed(() => {
    const chart = this.chart();
    const expanded = this.expandedGraphId();
    if (expanded && chart.subgraphs[expanded]) return chart.subgraphs[expanded];
    return chart.global_graph;
  });
  readonly isExpanded = computed(() => this.currentGraph().graph_id !== 'global');
  readonly activeNode = computed(() => {
    const graph = this.currentGraph();
    return graph.nodes.find((node) => node.id === graph.primary_node_id) ?? null;
  });
  readonly nodes = computed<Node<LifecycleChartNode>[]>(() => {
    const graph = this.currentGraph();
    return createNodes(
      graph.nodes.map((node, index) => ({
        id: node.id,
        type: 'html-template' as const,
        point: this.nodePoint(graph, node, index),
        width: NODE_WIDTH,
        height: NODE_HEIGHT,
        draggable: false,
        data: node,
      })),
      { useDefaults: true },
    );
  });
  readonly edges = computed<Edge<LifecycleChartEdge>[]>(() => {
    return createEdges(
      this.currentGraph().edges.map((edge) => ({
        id: edge.id,
        source: edge.source,
        target: edge.target,
        type: 'template' as const,
        curve: 'smooth-step' as const,
        data: edge,
        floating: true,
        markers: {
          end: {
            type: 'arrow-closed' as const,
            width: 18,
            height: 18,
            color: this.edgeColor(edge.status),
          },
        },
      })),
      { useDefaults: true },
    );
  });

  collapse(): void {
    this.expandedGraphId.set(null);
  }

  expandNode(node: LifecycleChartNode): void {
    if (!node.expandable || !node.subgraph_id || !this.chart().subgraphs[node.subgraph_id]) return;
    this.expandedGraphId.set(node.subgraph_id);
  }

  onNodeKeydown(event: KeyboardEvent, node: LifecycleChartNode): void {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    this.expandNode(node);
  }

  invokeAction(action: LifecycleChartAction): void {
    if (!action.enabled || this.busyAction() !== null) return;
    this.actionInvoked.emit(action.id);
  }

  nodeData(ctx: VflowDataContext<LifecycleChartNode>): LifecycleChartNode {
    return ctx.data();
  }

  edgeData(ctx: VflowDataContext<LifecycleChartEdge>): LifecycleChartEdge {
    return ctx.data();
  }

  statusLabel(status: LifecycleChartStatus): string {
    switch (status) {
      case 'passed':
        return 'Clear';
      case 'active':
        return 'Here now';
      case 'blocked':
        return 'Blocked';
      case 'poison':
        return 'Poisoned';
      case 'freeze':
        return 'Frozen';
      case 'inactive':
        return 'Waiting';
      case 'unknown':
        return 'Unknown';
    }
  }

  edgeColor(status: LifecycleChartStatus): string {
    switch (status) {
      case 'passed':
        return '#2f8f63';
      case 'active':
        return '#2563eb';
      case 'blocked':
        return '#b7791f';
      case 'poison':
        return '#b91c1c';
      case 'freeze':
        return '#7c3aed';
      case 'unknown':
        return '#64748b';
      case 'inactive':
        return '#cbd5e1';
    }
  }

  private nodePoint(graph: LifecycleChartGraph, node: LifecycleChartNode, index: number): Point {
    if (graph.graph_id === 'global') return GLOBAL_LAYOUT[node.id] ?? this.fallbackPoint(index);
    return this.focusedPoint(index);
  }

  private focusedPoint(index: number): Point {
    return {
      x: 40 + index * 260,
      y: index % 2 === 0 ? 80 : 170,
    };
  }

  private fallbackPoint(index: number): Point {
    return {
      x: 40 + (index % 4) * 250,
      y: 60 + Math.floor(index / 4) * 150,
    };
  }

}
