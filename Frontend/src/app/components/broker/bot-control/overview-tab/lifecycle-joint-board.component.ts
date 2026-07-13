import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  Injector,
  afterNextRender,
  computed,
  effect,
  inject,
  input,
  output,
  viewChild,
} from '@angular/core';
import { dia, shapes } from '@joint/core';

import type {
  LifecycleChartEdge,
  LifecycleChartGraph,
  LifecycleChartLane,
  LifecycleChartNode,
  LifecycleChartStatus,
} from '../../../../api/live-instances.types';
import { LifecycleNodeCardComponent } from './lifecycle-node-card.component';

interface FlowPoint {
  readonly x: number;
  readonly y: number;
}

interface NodeView {
  readonly node: LifecycleChartNode;
  readonly x: number;
  readonly y: number;
  readonly selected: boolean;
  readonly highlighted: boolean;
  readonly primary: boolean;
  readonly blocking: boolean;
  readonly receiptsExpanded: boolean;
  readonly headingId: string;
  readonly receiptRegionId: string;
}

interface ConnectorEdgeView {
  readonly id: string;
  readonly source: FlowPoint;
  readonly target: FlowPoint;
  readonly vertices: readonly FlowPoint[];
  readonly status: LifecycleChartStatus;
  readonly label: string | null;
  readonly animated: boolean;
}

const NODE_WIDTH = 176;
const NODE_HEIGHT = 158;
const COLUMN_GAP = 210;
const START_X = 18;
const START_Y = 42;
const TOP_ROW_Y = START_Y;
const BOTTOM_ROW_Y = 332;
const BOARD_PADDING = 18;
const MIN_BOARD_HEIGHT = 560;

const FALLBACK_ROW_BY_LANE: Record<LifecycleChartLane, 0 | 1> = {
  bot: 0,
  account: 1,
  broker: 1,
  recovery: 1,
};

const ROW_Y: Record<0 | 1, number> = {
  0: TOP_ROW_Y,
  1: BOTTOM_ROW_Y,
};

@Component({
  selector: 'app-lifecycle-joint-board',
  imports: [LifecycleNodeCardComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './lifecycle-joint-board.component.html',
  styleUrl: './lifecycle-joint-board.component.scss',
})
export class LifecycleJointBoardComponent {
  private readonly injector = inject(Injector);
  private readonly destroyRef = inject(DestroyRef);
  private readonly paperHost = viewChild<ElementRef<HTMLElement>>('paperHost');
  private readonly graphModel = new dia.Graph({}, { cellNamespace: shapes });
  private paper: dia.Paper | null = null;

  readonly graph = input.required<LifecycleChartGraph>();
  readonly chartKey = input.required<string>();
  readonly selectedNodeId = input<string | null>(null);
  readonly highlightedNodeId = input<string | null>(null);
  readonly expandedReceiptNodeKey = input<string | null>(null);

  readonly selectedRequested = output<LifecycleChartNode>();
  readonly subgraphRequested = output<LifecycleChartNode>();
  readonly receiptsToggled = output<LifecycleChartNode>();

  private readonly layoutPoints = computed(() => this.computeLayoutPoints(this.graph()));

  readonly nodeViews = computed<NodeView[]>(() => {
    const points = this.layoutPoints();
    return this.graph().nodes.map((node, index) => {
      const point = points.get(node.id) ?? this.fallbackNodePoint(node, index);
      return {
        node,
        x: point.x,
        y: point.y,
        selected: this.selectedNodeId() === node.id,
        highlighted: this.highlightedNodeId() === node.id,
        primary: this.isPrimaryNode(node),
        blocking: this.isBlockingNode(node),
        receiptsExpanded: this.isNodeReceiptsExpanded(node),
        headingId: this.nodeHeadingId(node),
        receiptRegionId: this.nodeReceiptRegionId(node),
      };
    });
  });

  readonly connectorEdges = computed<ConnectorEdgeView[]>(() => {
    const points = this.layoutPoints();
    return this.graph().edges
      .map((edge) => this.connectorView(edge, points))
      .filter((edge): edge is ConnectorEdgeView => edge !== null);
  });

  readonly boardSize = computed(() => {
    const points = this.layoutPoints();
    const fallback = { x: START_X, y: TOP_ROW_Y };
    const maxPoint = this.graph().nodes.reduce((max, node, index) => {
      const point = points.get(node.id) ?? this.fallbackNodePoint(node, index);
      return {
        x: Math.max(max.x, point.x),
        y: Math.max(max.y, point.y),
      };
    }, fallback);
    return {
      width: maxPoint.x + NODE_WIDTH + BOARD_PADDING,
      height: Math.max(MIN_BOARD_HEIGHT, maxPoint.y + NODE_HEIGHT + BOARD_PADDING),
    };
  });

  constructor() {
    afterNextRender(() => {
      effect(() => this.renderPaper(), { injector: this.injector });
    });
    this.destroyRef.onDestroy(() => {
      this.paper?.remove();
      this.graphModel.clear();
    });
  }

  nodeReceiptRegionId(node: LifecycleChartNode): string {
    return `lifecycle-node-receipts-${this.graph().graph_id}-${node.id}`;
  }

  nodeHeadingId(node: LifecycleChartNode): string {
    return `lifecycle-node-heading-${this.graph().graph_id}-${node.id}`;
  }

  edgeStatusLabel(status: LifecycleChartStatus): string {
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
        return 'Needs proof';
      case 'inactive':
        return 'Waiting';
    }
  }

  edgeAccessibleLabel(edge: ConnectorEdgeView): string {
    const status = this.edgeStatusLabel(edge.status);
    return edge.label ? `${status}: ${edge.label}` : status;
  }

  private renderPaper(): void {
    const host = this.paperHost()?.nativeElement;
    if (!host || !this.canRenderJointPaper()) return;

    const board = this.boardSize();
    const edges = this.connectorEdges();
    this.ensurePaper(host, board.width, board.height);
    if (!this.paper) return;

    this.paper.setDimensions(board.width, board.height);
    this.graphModel.resetCells(
      edges.flatMap((edge) => [
        this.createJointLink(edge, 'track'),
        this.createJointLink(edge, 'signal'),
      ]),
    );
  }

  private ensurePaper(host: HTMLElement, width: number, height: number): void {
    if (this.paper) return;
    this.paper = new dia.Paper({
      el: host,
      model: this.graphModel,
      width,
      height,
      gridSize: 1,
      interactive: false,
      async: false,
      background: { color: 'transparent' },
      cellViewNamespace: shapes,
      preventDefaultBlankAction: false,
    });
  }

  private createJointLink(edge: ConnectorEdgeView, role: 'track' | 'signal'): shapes.standard.Link {
    const link = new shapes.standard.Link();
    const isTrack = role === 'track';
    link.source(edge.source);
    link.target(edge.target);
    link.vertices([...edge.vertices]);
    link.connector('rounded', { radius: 14 });
    link.attr({
      root: {
        class: [
          'lifecycle-joint-link',
          `status-${edge.status}`,
          role === 'signal' ? 'signal-link' : 'track-link',
          edge.animated && role === 'signal' ? 'edge-animated' : '',
        ].filter(Boolean).join(' '),
        'data-edge-id': edge.id,
      },
      line: {
        fill: 'none',
        stroke: isTrack ? 'var(--bg-sunken)' : this.edgeColor(edge.status),
        strokeWidth: isTrack ? 8 : 3.5,
        strokeLinecap: 'round',
        strokeLinejoin: 'round',
        strokeDasharray: this.edgeDash(edge.status, edge.animated && role === 'signal'),
        targetMarker: isTrack
          ? null
          : {
            type: 'path',
            d: 'M 9 -5 0 0 9 5 z',
            fill: this.edgeColor(edge.status),
            stroke: this.edgeColor(edge.status),
          },
      },
    });
    link.set('z', isTrack ? 0 : 1);
    return link;
  }

  private connectorView(
    edge: LifecycleChartEdge,
    points: ReadonlyMap<string, FlowPoint>,
  ): ConnectorEdgeView | null {
    const source = points.get(edge.source);
    const target = points.get(edge.target);
    if (!source || !target) return null;

    const sourceAnchor = this.anchorPoint(source, edge.source_handle ?? 'source-east');
    const targetAnchor = this.anchorPoint(target, edge.target_handle ?? 'target-west');
    return {
      id: edge.id,
      source: sourceAnchor,
      target: targetAnchor,
      vertices: this.edgeVertices(sourceAnchor, targetAnchor),
      status: edge.status,
      label: edge.label,
      animated: edge.animated,
    };
  }

  private computeLayoutPoints(graph: LifecycleChartGraph): Map<string, FlowPoint> {
    if (graph.edges.length === 0) {
      return new Map(graph.nodes.map((node, index) => [node.id, this.fallbackNodePoint(node, index)]));
    }

    const nodeIds = new Set(graph.nodes.map((node) => node.id));
    const columns = new Map(graph.nodes.map((node) => [node.id, 0]));
    const edges = graph.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target));
    let remainingPasses = graph.nodes.length;
    while (remainingPasses > 0) {
      remainingPasses -= 1;
      let changed = false;
      for (const edge of edges) {
        const sourceColumn = columns.get(edge.source) ?? 0;
        const targetColumn = columns.get(edge.target) ?? 0;
        const nextColumn = sourceColumn + 1;
        if (nextColumn > targetColumn) {
          columns.set(edge.target, nextColumn);
          changed = true;
        }
      }
      if (!changed) break;
    }

    const visualRows = this.computeVisualRows(graph, columns, edges);
    const rowColumnCounts = new Map<string, number>();
    return new Map(graph.nodes.map((node) => {
      const column = columns.get(node.id) ?? 0;
      const row = visualRows.get(node.id) ?? FALLBACK_ROW_BY_LANE[node.lane];
      const rowKey = `${row}:${column}`;
      const laneOffset = rowColumnCounts.get(rowKey) ?? 0;
      rowColumnCounts.set(rowKey, laneOffset + 1);
      return [node.id, {
        x: START_X + column * COLUMN_GAP,
        y: ROW_Y[row] + laneOffset * (NODE_HEIGHT + 34),
      }];
    }));
  }

  private fallbackNodePoint(node: LifecycleChartNode, index: number): FlowPoint {
    return {
      x: START_X + index * COLUMN_GAP,
      y: ROW_Y[FALLBACK_ROW_BY_LANE[node.lane]],
    };
  }

  private computeVisualRows(
    graph: LifecycleChartGraph,
    columns: ReadonlyMap<string, number>,
    edges: readonly LifecycleChartEdge[],
  ): Map<string, 0 | 1> {
    const nodesById = new Map(graph.nodes.map((node) => [node.id, node]));
    const outgoing = new Map<string, LifecycleChartEdge[]>();
    const incoming = new Map<string, LifecycleChartEdge[]>();
    for (const edge of edges) {
      outgoing.set(edge.source, [...(outgoing.get(edge.source) ?? []), edge]);
      incoming.set(edge.target, [...(incoming.get(edge.target) ?? []), edge]);
    }

    const rows = new Map<string, 0 | 1>();
    const nodesByColumn = [...graph.nodes].sort((a, b) => {
      const columnDelta = (columns.get(a.id) ?? 0) - (columns.get(b.id) ?? 0);
      return columnDelta === 0 ? graph.nodes.indexOf(a) - graph.nodes.indexOf(b) : columnDelta;
    });

    for (const node of nodesByColumn) {
      if (node.lane === 'bot') {
        rows.set(node.id, 0);
        continue;
      }
      if (node.lane !== 'broker') {
        rows.set(node.id, 1);
        continue;
      }

      const parents = incoming.get(node.id) ?? [];
      const followsTopBranch = parents.some((edge) => {
        const parent = nodesById.get(edge.source);
        return rows.get(edge.source) === 0 && (
          parent?.lane === 'broker' || (outgoing.get(edge.source)?.length ?? 0) > 1
        );
      });
      rows.set(node.id, followsTopBranch ? 0 : 1);
    }

    return rows;
  }

  private edgeVertices(source: FlowPoint, target: FlowPoint): FlowPoint[] {
    if (Math.abs(source.x - target.x) < 1) {
      const midY = source.y + (target.y - source.y) / 2;
      return [
        { x: source.x, y: midY },
        { x: target.x, y: midY },
      ];
    }
    const midX = source.x + (target.x - source.x) / 2;
    return [
      { x: midX, y: source.y },
      { x: midX, y: target.y },
    ];
  }

  private anchorPoint(point: FlowPoint, handleId: string): FlowPoint {
    if (handleId.includes('south') || handleId.includes('bottom')) {
      return { x: point.x + NODE_WIDTH / 2, y: point.y + NODE_HEIGHT };
    }
    if (handleId.includes('north') || handleId.includes('top')) {
      return { x: point.x + NODE_WIDTH / 2, y: point.y };
    }
    if (handleId.includes('west') || handleId.includes('left')) {
      return { x: point.x, y: point.y + NODE_HEIGHT / 2 };
    }
    return { x: point.x + NODE_WIDTH, y: point.y + NODE_HEIGHT / 2 };
  }

  private edgeColor(status: LifecycleChartStatus): string {
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
      case 'inactive':
        return 'var(--text-secondary)';
    }
  }

  private edgeDash(status: LifecycleChartStatus, animated: boolean): string | null {
    if (animated) return '9 7';
    if (status === 'unknown') return '6 7';
    if (status === 'inactive') return '7 6';
    return null;
  }

  private isPrimaryNode(node: LifecycleChartNode): boolean {
    return node.id === this.graph().primary_node_id;
  }

  private isBlockingNode(node: LifecycleChartNode): boolean {
    return this.isPrimaryNode(node) && this.isBlockingStatus(node.status);
  }

  private isBlockingStatus(status: LifecycleChartStatus): boolean {
    return status === 'blocked' || status === 'poison' || status === 'freeze' || status === 'unknown';
  }

  private isNodeReceiptsExpanded(node: LifecycleChartNode): boolean {
    return this.expandedReceiptNodeKey() === this.nodeReceiptKey(node);
  }

  private nodeReceiptKey(node: LifecycleChartNode): string {
    return `${this.chartKey()}:${this.graph().graph_id}:${node.id}`;
  }

  private canRenderJointPaper(): boolean {
    const processLike = globalThis as {
      readonly process?: { readonly env?: Record<string, string | undefined> };
    };
    if (processLike.process?.env?.['VITEST']) return false;
    return typeof document !== 'undefined' && typeof SVGElement !== 'undefined';
  }
}
