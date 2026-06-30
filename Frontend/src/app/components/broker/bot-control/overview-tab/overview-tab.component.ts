import { CommonModule } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, input, output, signal, type Signal } from '@angular/core';
import { createEdges, createNodes, type Edge, type Node, Vflow } from 'ngx-vflow';

import type {
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

interface ExpandedGraphSelection {
  readonly chartKey: string;
  readonly graphId: string;
}

const NODE_WIDTH = 190;
const NODE_HEIGHT = 96;

const GLOBAL_LAYOUT: Record<string, Point> = {
  deploy: { x: 40, y: 36 },
  preflight: { x: 40, y: 184 },
  account_safety: { x: 40, y: 332 },
  reconcile: { x: 40, y: 480 },
  activate: { x: 40, y: 628 },
  active: { x: 40, y: 776 },
  submit_order: { x: 300, y: 776 },
  broker_writer: { x: 300, y: 924 },
  recovery: { x: 40, y: 924 },
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
  readonly selectedNodeId = input<string | null>(null);
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

  nodeData(ctx: VflowDataContext<LifecycleChartNode>): LifecycleChartNode {
    return ctx.data();
  }

  edgeData(ctx: VflowDataContext<LifecycleChartEdge>): LifecycleChartEdge {
    return ctx.data();
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

  private nodePoint(graph: LifecycleChartGraph, node: LifecycleChartNode, index: number): Point {
    if (graph.graph_id === 'global') return GLOBAL_LAYOUT[node.id] ?? this.fallbackPoint(index);
    return this.focusedPoint(index);
  }

  private focusedPoint(index: number): Point {
    return {
      x: 80,
      y: 40 + index * 148,
    };
  }

  private fallbackPoint(index: number): Point {
    return {
      x: 80,
      y: 40 + index * 148,
    };
  }

}
