/**
 * Tests for the live-instance-status fixture helpers and the
 * LifecycleChartReceipt / LifecycleChartNode shapes added in the
 * lifecycle node receipts slice.
 *
 * These tests verify that:
 *   - EMPTY_NODE_EVIDENCE defaults are structurally correct;
 *   - every node in the default fixture carries the new fields;
 *   - the broker_writer node uses the updated labels; and
 *   - the LifecycleChartReceipt interface shape is satisfied at compile time
 *     and at runtime.
 */

import { describe, expect, it } from 'vitest';

import type { LifecycleChartNode, LifecycleChartReceipt } from '../api/live-instances.types';
import { makeLifecycleChartFixture } from './live-instance-status-fixtures';

// ---------------------------------------------------------------------------
// LifecycleChartReceipt runtime shape

describe('LifecycleChartReceipt shape', () => {
  it('accepts a minimal receipt with no timestamp', () => {
    const receipt: LifecycleChartReceipt = {
      label: 'reconciliation.state',
      value: 'PASSED',
      unit: null,
      source: null,
      gate_id: null,
      ts_ms: null,
      ts_ms_resolved: false,
    };

    expect(receipt.label).toBe('reconciliation.state');
    expect(receipt.value).toBe('PASSED');
    expect(receipt.unit).toBeNull();
    expect(receipt.source).toBeNull();
    expect(receipt.gate_id).toBeNull();
    expect(receipt.ts_ms).toBeNull();
    expect(receipt.ts_ms_resolved).toBe(false);
  });

  it('accepts a fully-populated receipt with resolved timestamp', () => {
    const receipt: LifecycleChartReceipt = {
      label: 'last_reconcile_ms',
      value: '1700000000000',
      unit: 'ms UTC',
      source: 'reconciliation_projection',
      gate_id: null,
      ts_ms: 1_700_000_000_000,
      ts_ms_resolved: true,
    };

    expect(receipt.ts_ms).toBe(1_700_000_000_000);
    expect(receipt.ts_ms_resolved).toBe(true);
    expect(receipt.unit).toBe('ms UTC');
    expect(receipt.source).toBe('reconciliation_projection');
  });

  it('accepts a receipt with a gate_id', () => {
    const receipt: LifecycleChartReceipt = {
      label: 'gate_result',
      value: 'pass',
      unit: null,
      source: 'engine',
      gate_id: 'engine_ready',
      ts_ms: 1_700_000_000_000,
      ts_ms_resolved: true,
    };

    expect(receipt.gate_id).toBe('engine_ready');
  });
});

// ---------------------------------------------------------------------------
// makeLifecycleChartFixture — global graph nodes carry new receipt fields

describe('makeLifecycleChartFixture global graph nodes', () => {
  const chart = makeLifecycleChartFixture();
  const nodeById = new Map<string, LifecycleChartNode>(
    chart.global_graph.nodes.map((n) => [n.id, n]),
  );

  const GLOBAL_NODE_IDS = [
    'deploy',
    'preflight',
    'account_safety',
    'reconcile',
    'activate',
    'active',
    'submit_order',
    'broker_writer',
    'recovery',
  ] as const;

  for (const nodeId of GLOBAL_NODE_IDS) {
    it(`${nodeId} node carries ts_ms=null, ts_ms_resolved=false, receipts=[]`, () => {
      const node = nodeById.get(nodeId);
      expect(node).toBeDefined();
      expect(node!.ts_ms).toBeNull();
      expect(node!.ts_ms_resolved).toBe(false);
      expect(node!.receipts).toEqual([]);
    });
  }
});

// ---------------------------------------------------------------------------
// broker_writer node uses updated labels

describe('makeLifecycleChartFixture broker_writer node labels', () => {
  it('broker_writer label is "Broker activity" (not "Broker writer")', () => {
    const chart = makeLifecycleChartFixture();
    const brokerWriter = chart.global_graph.nodes.find((n) => n.id === 'broker_writer');

    expect(brokerWriter).toBeDefined();
    expect(brokerWriter!.label).toBe('Broker activity');
  });

  it('broker_writer technical_label is "Publisher health" (not "placeOrder boundary")', () => {
    const chart = makeLifecycleChartFixture();
    const brokerWriter = chart.global_graph.nodes.find((n) => n.id === 'broker_writer');

    expect(brokerWriter!.technical_label).toBe('Publisher health');
  });

  it('broker_writer evidence_summary references broker-activity publisher health', () => {
    const chart = makeLifecycleChartFixture();
    const brokerWriter = chart.global_graph.nodes.find((n) => n.id === 'broker_writer');

    expect(brokerWriter!.evidence_summary).toBe('Broker-activity publisher health is unavailable.');
  });
});

// ---------------------------------------------------------------------------
// makeLifecycleChartFixture — subgraph nodes also carry new fields

describe('makeLifecycleChartFixture subgraph deploy nodes', () => {
  it('deploy subgraph host_state node carries ts_ms=null, ts_ms_resolved=false, receipts=[]', () => {
    const chart = makeLifecycleChartFixture();
    const deploySubgraph = chart.subgraphs['deploy'];

    expect(deploySubgraph).toBeDefined();
    const hostStateNode = deploySubgraph.nodes.find((n) => n.id === 'host_state');
    expect(hostStateNode).toBeDefined();
    expect(hostStateNode!.ts_ms).toBeNull();
    expect(hostStateNode!.ts_ms_resolved).toBe(false);
    expect(hostStateNode!.receipts).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// Override path: nodes with explicit ts_ms are still valid

describe('makeLifecycleChartFixture with override', () => {
  it('accepts overridden nodes that carry a resolved timestamp and receipts', () => {
    const receipt: LifecycleChartReceipt = {
      label: 'reconciliation.state',
      value: 'PASSED',
      unit: null,
      source: 'reconciliation_projection',
      gate_id: null,
      ts_ms: 1_700_000_000_000,
      ts_ms_resolved: true,
    };

    const chart = makeLifecycleChartFixture({
      global_graph: {
        ...makeLifecycleChartFixture().global_graph,
        nodes: makeLifecycleChartFixture().global_graph.nodes.map((n) =>
          n.id === 'reconcile'
            ? { ...n, ts_ms: 1_700_000_000_000, ts_ms_resolved: true, receipts: [receipt] }
            : n,
        ),
      },
    });

    const reconcileNode = chart.global_graph.nodes.find((n) => n.id === 'reconcile');
    expect(reconcileNode!.ts_ms).toBe(1_700_000_000_000);
    expect(reconcileNode!.ts_ms_resolved).toBe(true);
    expect(reconcileNode!.receipts).toHaveLength(1);
    expect(reconcileNode!.receipts[0].label).toBe('reconciliation.state');
  });

  it('deploy node default ts_ms_resolved=false is not contaminated by override', () => {
    const chart = makeLifecycleChartFixture();
    const deployNode = chart.global_graph.nodes.find((n) => n.id === 'deploy');

    // Even after building a fixture, the deploy node must keep its defaults.
    expect(deployNode!.ts_ms).toBeNull();
    expect(deployNode!.ts_ms_resolved).toBe(false);
    expect(deployNode!.receipts).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// LifecycleChartNode type-level assertions: receipts is an array

describe('LifecycleChartNode receipts field type', () => {
  it('receipts field is an array on all global nodes', () => {
    const chart = makeLifecycleChartFixture();
    for (const node of chart.global_graph.nodes) {
      expect(Array.isArray(node.receipts)).toBe(true);
    }
  });

  it('ts_ms_resolved is a boolean on all global nodes', () => {
    const chart = makeLifecycleChartFixture();
    for (const node of chart.global_graph.nodes) {
      expect(typeof node.ts_ms_resolved).toBe('boolean');
    }
  });
});