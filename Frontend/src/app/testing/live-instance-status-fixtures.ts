import type { BotLifecycleChartView } from '../api/live-instances.types';

export function makeLifecycleChartFixture(
  overrides: Partial<BotLifecycleChartView> = {},
): BotLifecycleChartView {
  const chart: BotLifecycleChartView = {
    chart_id: 'bot_lifecycle_v1',
    selected_bot_id: 'sid-x',
    title: 'Bot lifecycle overview',
    global_graph: {
      graph_id: 'global',
      title: 'Bot lifecycle overview',
      primary_node_id: 'deploy',
      nodes: [
        {
          id: 'deploy',
          label: 'Deploy or start',
          technical_label: 'Host process',
          lane: 'bot',
          status: 'active',
          expandable: true,
          subgraph_id: 'deploy',
          evidence_summary: 'Backend start gate is ready.',
        },
      ],
      edges: [],
    },
    subgraphs: {
      deploy: {
        graph_id: 'deploy',
        title: 'Deploy and start internals',
        primary_node_id: 'host_state',
        nodes: [
          {
            id: 'host_state',
            label: 'Host state',
            technical_label: 'IDLE',
            lane: 'bot',
            status: 'active',
            expandable: false,
            subgraph_id: null,
            evidence_summary: 'Daemon is reachable.',
          },
        ],
        edges: [],
      },
    },
    actions: [
      {
        id: 'start_process',
        label: 'Start bot process',
        enabled: true,
        reason: 'Backend-authored start request is ready.',
        target_node_id: 'deploy',
        tone: 'primary',
      },
    ],
  };
  return { ...chart, ...overrides };
}
