// PRD #607 / Slice 1 (#608) — server <-> Frontend contract test.
//
// Snapshots captured from the running Python ``/api/live-instances/{id}/status``
// endpoint via ``PythonDataService/scripts/capture_operator_surface_fixture.py``.
// A Python freshness test re-captures the route and compares it to the committed
// JSON. This Vitest contract then checks those same JSON payloads against the
// Frontend status type so a shape drift (renamed field, dropped block,
// null/non-null mismatch) becomes a TypeScript build failure, NOT a silent
// runtime gap.
//
// To refresh: run the Python script after any projection change, then
// commit both the Python diff and the regenerated JSON fixtures in the same PR.

import { describe, expect, it } from "vitest";

import steadyFixture from "../../testing/operator_surface_fixtures/steady.json";
import stoppedFixture from "../../testing/operator_surface_fixtures/stopped.json";
import type {
  AccountOwnerPhase,
  ActionEffect,
  ActionPlanConsumption,
  BrokerActivityHealth,
  BrokerConnectionState,
  BrokerObservationConsistency,
  BrokerSafetyVerdict,
  DaemonResultKind,
  ExecutionPosture,
  GateResult,
  GateResultStatus,
  HostProcessStartDisabledReasonCode,
  HostProcessState,
  InstanceProcessState,
  LatestSignalTone,
  LifecycleChartAction,
  LifecycleChartActionability,
  LifecycleChartActionId,
  LifecycleChartLane,
  LifecycleChartStatus,
  LiveInstanceStatus,
  OperatorNotice,
  OperatorNoticeActionKind,
  OperatorNoticeCode,
  OperatorNoticeTier,
  OperatorSurface,
  OperatorVerdict,
  ReadinessGate,
  ReadinessVector,
  ReconciliationState,
  RiskPosture,
  RuntimeFreshnessState,
  SubmitReadinessCode,
  TraderAttentionSeverity,
  TraderPrimaryRemediation,
  TraderSituationCode,
  TradingSessionPhase,
} from "./live-instances.types";

// TypeScript widens JSON string literals to `string`, so raw JSON cannot
// satisfy closed string unions directly. The Python freshness test anchors
// literal values to backend output; this helper keeps the structural and
// nullability contract checked against the Frontend type.
type JsonImported<T> = T extends string
  ? string
  : T extends number
    ? number
    : T extends boolean
      ? boolean
      : T extends readonly (infer Item)[]
        ? JsonImported<Item>[]
        : T extends object
          ? { [Key in keyof T]: JsonImported<T[Key]> }
          : T;

const STEADY: JsonImported<LiveInstanceStatus> = steadyFixture;
const STOPPED: JsonImported<LiveInstanceStatus> = stoppedFixture;
const FIXTURES = [
  ["steady", STEADY],
  ["stopped", STOPPED],
] as const;

const INSTANCE_PROCESS_STATES = [
  "running",
  "stopping",
  "exited",
  "idle",
  "unreachable",
] as const satisfies readonly InstanceProcessState[];
const READINESS_KINDS = [
  "live_readiness",
  "start_readiness",
] as const satisfies readonly ReadinessVector["kind"][];
const READINESS_SOURCES = [
  "engine",
  "backend_derived",
] as const satisfies readonly ReadinessVector["source"][];
const READINESS_VERDICTS = [
  "READY",
  "BLOCKED",
  "DEGRADED",
  "UNKNOWN",
] as const satisfies readonly ReadinessVector["verdict"][];
const READINESS_GATE_STATUSES = [
  "pass",
  "fail",
  "unknown",
] as const satisfies readonly ReadinessGate["status"][];
const READINESS_GATE_SEVERITIES = [
  "hard",
  "soft",
] as const satisfies readonly ReadinessGate["severity"][];
const LATEST_SIGNAL_TONES = [
  "ok",
  "warn",
  "neutral",
] as const satisfies readonly LatestSignalTone[];
const HOST_PROCESS_STATES = [
  "RUNNING",
  "STOPPING",
  "EXITED",
  "IDLE",
  "WAITING_FOR_HOST",
  "UNREACHABLE",
] as const satisfies readonly HostProcessState[];
const START_DISABLED_REASON_CODES = [
  "ALREADY_RUNNING",
  "STOPPING",
  "HOST_SERVICE_OFFLINE",
  "STOPPED_REQUIRES_REDEPLOY",
  "START_SETTINGS_INCOMPLETE",
  "ACCOUNT_FROZEN",
] as const satisfies readonly HostProcessStartDisabledReasonCode[];
const BROKER_SAFETY_VERDICTS = [
  "PAPER_ONLY",
  "UNSAFE",
  "UNKNOWN",
] as const satisfies readonly BrokerSafetyVerdict[];
const BROKER_CONNECTION_STATES = [
  "CONNECTED",
  "DISCONNECTED",
  "DEGRADED",
  "UNKNOWN",
] as const satisfies readonly BrokerConnectionState[];
const EXECUTION_POSTURES = [
  "PAPER_EXECUTION",
  "READ_ONLY",
  "UNSAFE",
  "UNKNOWN",
] as const satisfies readonly ExecutionPosture[];
const TRADING_SESSION_PHASES = [
  "PRE",
  "RTH",
  "POST",
  "CLOSED",
  "UNKNOWN",
] as const satisfies readonly TradingSessionPhase[];
const ACCOUNT_OWNER_PHASES = [
  "accepting",
  "reconnecting",
  "draining",
  "frozen",
  "unknown",
] as const satisfies readonly AccountOwnerPhase[];
const SUBMIT_READINESS_CODES = [
  "safe_to_submit",
  "safe_to_monitor",
  "blocked_before_submit",
  "broker_state_unproven",
  "account_frozen",
  "waiting_for_owner_generation",
  "submit_outcome_uncertain",
] as const satisfies readonly SubmitReadinessCode[];
const TRADER_SITUATION_CODES = [
  "ready_to_submit",
  "monitor_only",
  "submission_blocked",
  "broker_state_unproven",
  "account_frozen",
  "waiting_for_owner_generation",
  "submit_outcome_uncertain",
  "attention_required",
  "unknown",
] as const satisfies readonly TraderSituationCode[];
const TRADER_ATTENTION_SEVERITIES = [
  "info",
  "warning",
  "critical",
] as const satisfies readonly TraderAttentionSeverity[];
const RISK_POSTURES = [
  "FLAT",
  "LONG",
  "SHORT",
  "MIXED",
  "UNKNOWN",
] as const satisfies readonly RiskPosture[];
const ACTION_PLAN_CONSUMPTIONS = [
  "ACTIVE",
  "DECLARATIVE_ONLY",
  "UNKNOWN",
] as const satisfies readonly ActionPlanConsumption[];
const ACTION_EFFECTS = [
  "DURABLE_ONLY",
  "LIVE_ACTUATION",
] as const satisfies readonly ActionEffect[];
const GATE_RESULT_STATUSES = [
  "pass",
  "block",
  "poison",
  "freeze",
  "unknown",
  "not_applicable",
] as const satisfies readonly GateResultStatus[];
const OPERATOR_VERDICTS = [
  "READY",
  "ATTENTION",
  "UNKNOWN",
] as const satisfies readonly OperatorVerdict[];
const RUNTIME_FRESHNESS_STATES = [
  "FRESH",
  "STALE",
  "NOT_APPLICABLE",
  "UNKNOWN",
  "DEGRADED",
] as const satisfies readonly RuntimeFreshnessState[];
const OPERATOR_NOTICE_TIERS = [
  "info",
  "warning",
  "critical",
] as const satisfies readonly OperatorNoticeTier[];
const OPERATOR_NOTICE_CODES = [
  "runtime.market_closed",
  "runtime.market_session_halted",
  "runtime.market_data_stale",
  "runtime.market_data_feed_stalled",
  "runtime.broker_probe_stale",
  "runtime.broker_probe_missing",
  "runtime.command_loop_unresponsive",
  "runtime.engine_runtime_incompatible",
  "runtime.control_plane_lease_stale",
  "runtime.control_plane_boot_id_mismatch",
  "watchdog.flatten_completed",
  "watchdog.flatten_not_needed",
  "watchdog.flatten_timed_out",
  "watchdog.flatten_failed",
  "watchdog.broker_disconnected_before_flatten",
  "activity.publisher_starting",
  "activity.publisher_not_running",
  "activity.publisher_degraded",
  "activity.source_blind_to_bot_orders",
  "activity.dropped_paused_intent",
  "reconciliation.required_after_uncertain_flatten",
  "reconciliation.discovered_execution_not_in_engine_state",
] as const satisfies readonly OperatorNoticeCode[];
const OPERATOR_NOTICE_ACTION_KINDS = [
  "none",
  "wait",
  "open_runbook",
  "focus_cockpit_action",
  "renew_control_plane_lease",
  "external_manual_check",
  "redeploy",
] as const satisfies readonly OperatorNoticeActionKind[];
const DAEMON_RESULT_KINDS = [
  "CONNECTED",
  "RETRYING",
  "UNREACHABLE",
  "AUTH_FAILED",
  "PROTOCOL_ERROR",
  "INCOMPATIBLE_CONTRACT",
] as const satisfies readonly DaemonResultKind[];
const BROKER_ACTIVITY_STATES = [
  "ready",
  "starting",
  "degraded",
  "unavailable",
] as const satisfies readonly BrokerActivityHealth["state"][];
const RECONCILIATION_STATES = [
  "NOT_AVAILABLE",
  "IN_PROGRESS",
  "CLEAN",
  "ADOPTED",
  "STALE",
  "FAILED",
] as const satisfies readonly ReconciliationState[];
const BROKER_OBSERVATION_VERDICTS = [
  "CONSISTENT",
  "CONFLICTING",
  "UNKNOWN",
  "NOT_COMPARABLE",
] as const satisfies readonly BrokerObservationConsistency["verdict"][];
const LIFECYCLE_CHART_STATUSES = [
  "passed",
  "active",
  "blocked",
  "poison",
  "freeze",
  "inactive",
  "unknown",
] as const satisfies readonly LifecycleChartStatus[];
const LIFECYCLE_CHART_LANES = [
  "bot",
  "account",
  "broker",
  "recovery",
] as const satisfies readonly LifecycleChartLane[];
const LIFECYCLE_ACTIONABILITIES = [
  "operator-actionable",
  "system-only",
  "no-action-needed",
] as const satisfies readonly LifecycleChartActionability[];
const LIFECYCLE_ACTION_IDS = [
  "start_process",
  "resume",
  "pause",
  "flatten_and_pause",
  "stop",
  "mark_poisoned",
  "redeploy",
] as const satisfies readonly LifecycleChartActionId[];
const LIFECYCLE_ACTION_TONES = [
  "primary",
  "secondary",
  "danger",
] as const satisfies readonly LifecycleChartAction["tone"][];
const TRADER_REMEDIATION_KINDS = [
  "invoke_capability",
  "focus_action",
  "redeploy",
  "open_runbook",
  "invoke_endpoint",
  "none",
] as const satisfies readonly TraderPrimaryRemediation["kind"][];

function assertOneOf<T extends string>(
  path: string,
  value: unknown,
  allowed: readonly T[],
): asserts value is T {
  expect(typeof value, path).toBe("string");
  expect(allowed, path).toContain(value as T);
}

function assertNullableOneOf<T extends string>(
  path: string,
  value: unknown,
  allowed: readonly T[],
): void {
  if (value === null) {
    return;
  }
  assertOneOf(path, value, allowed);
}

function assertGateResult(path: string, gate: JsonImported<GateResult>): void {
  assertOneOf(`${path}.status`, gate.status, GATE_RESULT_STATUSES);
}

function assertNotice(
  path: string,
  notice: JsonImported<OperatorNotice> | null,
): void {
  if (notice === null) {
    return;
  }
  assertOneOf(`${path}.code`, notice.code, OPERATOR_NOTICE_CODES);
  assertOneOf(`${path}.tier`, notice.tier, OPERATOR_NOTICE_TIERS);
  assertOneOf(
    `${path}.action.kind`,
    notice.action.kind,
    OPERATOR_NOTICE_ACTION_KINDS,
  );
}

function assertRemediation(
  path: string,
  remediation: JsonImported<TraderPrimaryRemediation>,
): void {
  assertOneOf(`${path}.kind`, remediation.kind, TRADER_REMEDIATION_KINDS);
}

function assertRuntimeFreshness(
  path: string,
  runtime: JsonImported<OperatorSurface["runtime_freshness"]>,
): void {
  if (runtime === null) {
    return;
  }
  for (const domain of [
    "command_loop",
    "broker",
    "bar_loop",
    "control_plane",
  ] as const) {
    assertOneOf(
      `${path}.${domain}.state`,
      runtime[domain].state,
      RUNTIME_FRESHNESS_STATES,
    );
  }
  assertNotice(`${path}.headline`, runtime.headline);
  runtime.additional_reasons.forEach((notice, index) =>
    assertNotice(`${path}.additional_reasons[${index}]`, notice),
  );
}

function assertLifecycleGraph(
  path: string,
  graph: JsonImported<LiveInstanceStatus["lifecycle_chart"]["global_graph"]>,
): void {
  graph.nodes.forEach((node, index) => {
    assertOneOf(
      `${path}.nodes[${index}].status`,
      node.status,
      LIFECYCLE_CHART_STATUSES,
    );
    assertOneOf(
      `${path}.nodes[${index}].lane`,
      node.lane,
      LIFECYCLE_CHART_LANES,
    );
    assertOneOf(
      `${path}.nodes[${index}].operator_actionability`,
      node.operator_actionability,
      LIFECYCLE_ACTIONABILITIES,
    );
  });
  graph.edges.forEach((edge, index) =>
    assertOneOf(
      `${path}.edges[${index}].status`,
      edge.status,
      LIFECYCLE_CHART_STATUSES,
    ),
  );
}

describe("live instance status fixture wire contract", () => {
  it("STEADY fixture carries every projection block", () => {
    expect(STEADY.operator_surface.schema_version).toBe(1);
    expect(STEADY.operator_surface.host_process.state).toBe("RUNNING");
    expect(STEADY.operator_surface.host_process.notice).toBeNull();
    expect(STEADY.operator_surface.host_process.copyable_command).toBeNull();
    expect(STEADY.operator_surface.actions.resume.enabled).toBe(false);
    expect(STEADY.operator_surface.actions.pause.enabled).toBe(true);
    expect(STEADY.operator_surface.actions.resume.disabled_reason_code).toBe(
      "POSTURE_DEMOTED",
    );
    expect(
      STEADY.operator_surface.actions.pause.disabled_reason_code,
    ).toBeNull();
    expect(STEADY.operator_surface.submit_readiness.code).toBe(
      "broker_state_unproven",
    );
    expect(STEADY.operator_surface.execution?.posture).toBe("UNKNOWN");
    expect(
      STEADY.operator_surface.trader_guidance.primary_remediation.kind,
    ).toBe("open_runbook");
    expect(
      STEADY.operator_surface.trader_guidance.primary_remediation,
    ).toMatchObject({
      slug: "broker-instance-operator-surface",
    });
  });

  it("STOPPED fixture surfaces the host-process notice and reflects the unbound state", () => {
    // Daemon-`idle` + the test fixture's default desired_state=RUNNING
    // upgrades the host-process state to WAITING_FOR_HOST; absent desired
    // intent it stays IDLE.  The fixture captures the unbound (idle)
    // case.  PRD #616 left this enum unchanged.
    expect(STOPPED.operator_surface.host_process.state).toBe("IDLE");
    expect(STOPPED.operator_surface.host_process.notice).toMatch(
      /no active process/i,
    );
    // flatten-and-pause requires a binding -> disabled with reason code.
    expect(STOPPED.operator_surface.actions.flatten_and_pause.enabled).toBe(
      false,
    );
    expect(
      STOPPED.operator_surface.actions.flatten_and_pause.disabled_reason_code,
    ).toBe("NO_LIVE_BINDING");
    // PRD #616 / runtime-freshness hardening — resume is fail-closed when
    // broker safety/submission capability are not proven.
    expect(STOPPED.operator_surface.actions.resume.enabled).toBe(false);
    expect(STOPPED.operator_surface.actions.resume.disabled_reason_code).toBe(
      "BROKER_SAFETY_UNKNOWN",
    );
    expect(STOPPED.operator_surface.actions.pause.enabled).toBe(true);
  });

  it("exposes the expected top-level keys on every fixture", () => {
    // PRD #607 (cockpit revision) added ``trading_session``; PRD #616
    // added ``readiness_gates``.  Both fixtures must carry the full
    // set so the Bot Control renderer cannot encounter a missing block.
    const expected = [
      "schema_version",
      "host_process",
      "prior_run",
      "broker",
      "configuration",
      "current_risk",
      "daily_order_cap",
      "action_plan",
      "account_owner",
      "submit_readiness",
      "trader_guidance",
      "actions",
      "trading_session",
      "readiness_gates",
      "runtime_freshness",
      "control_plane",
      "broker_observation_consistency",
      "reconciliation",
      "broker_activity_health",
      "incident_headline",
      "execution",
    ];
    for (const fixture of [STEADY, STOPPED]) {
      const actual = Object.keys(fixture.operator_surface).sort();
      expect(actual).toEqual([...expected].sort());
    }
  });

  it("every action capability carries the disabled_reasons list (PRD #616)", () => {
    for (const fixture of [STEADY, STOPPED]) {
      for (const action of Object.values(fixture.operator_surface.actions)) {
        expect(Array.isArray(action.disabled_reasons)).toBe(true);
      }
    }
  });

  it("exposes the five canonical actions including stop (PRD #616 / ADR-0010 §A1)", () => {
    const expected = new Set([
      "resume",
      "pause",
      "stop",
      "flatten_and_pause",
      "mark_poisoned",
    ]);
    for (const fixture of [STEADY, STOPPED]) {
      expect(new Set(Object.keys(fixture.operator_surface.actions))).toEqual(
        expected,
      );
    }
  });

  it("keeps backend-authored closed union literals inside the frontend contract", () => {
    for (const [fixtureName, fixture] of FIXTURES) {
      const surface = fixture.operator_surface;
      assertOneOf(
        `${fixtureName}.process.state`,
        fixture.process.state,
        INSTANCE_PROCESS_STATES,
      );
      assertOneOf(
        `${fixtureName}.latest_signal_tone`,
        fixture.latest_signal_tone,
        LATEST_SIGNAL_TONES,
      );
      if (fixture.readiness !== null) {
        assertOneOf(
          `${fixtureName}.readiness.kind`,
          fixture.readiness.kind,
          READINESS_KINDS,
        );
        assertOneOf(
          `${fixtureName}.readiness.source`,
          fixture.readiness.source,
          READINESS_SOURCES,
        );
        assertOneOf(
          `${fixtureName}.readiness.verdict`,
          fixture.readiness.verdict,
          READINESS_VERDICTS,
        );
        fixture.readiness.gates.forEach((gate, index) => {
          assertOneOf(
            `${fixtureName}.readiness.gates[${index}].status`,
            gate.status,
            READINESS_GATE_STATUSES,
          );
          assertOneOf(
            `${fixtureName}.readiness.gates[${index}].severity`,
            gate.severity,
            READINESS_GATE_SEVERITIES,
          );
          if (gate.gate_result !== null && gate.gate_result !== undefined) {
            assertGateResult(
              `${fixtureName}.readiness.gates[${index}].gate_result`,
              gate.gate_result,
            );
          }
        });
      }

      assertOneOf(
        `${fixtureName}.operator_surface.host_process.state`,
        surface.host_process.state,
        HOST_PROCESS_STATES,
      );
      assertNullableOneOf(
        `${fixtureName}.operator_surface.host_process.start_capability.disabled_reason_code`,
        surface.host_process.start_capability.disabled_reason_code,
        START_DISABLED_REASON_CODES,
      );
      surface.host_process.start_capability.gate_results.forEach(
        (gate, index) =>
          assertGateResult(
            `${fixtureName}.operator_surface.host_process.start_capability.gate_results[${index}]`,
            gate,
          ),
      );
      assertOneOf(
        `${fixtureName}.operator_surface.broker.safety_verdict`,
        surface.broker.safety_verdict,
        BROKER_SAFETY_VERDICTS,
      );
      assertOneOf(
        `${fixtureName}.operator_surface.broker.connection`,
        surface.broker.connection,
        BROKER_CONNECTION_STATES,
      );
      if (surface.execution !== null && surface.execution !== undefined) {
        assertOneOf(
          `${fixtureName}.operator_surface.execution.posture`,
          surface.execution.posture,
          EXECUTION_POSTURES,
        );
      }
      assertOneOf(
        `${fixtureName}.operator_surface.trading_session.phase`,
        surface.trading_session.phase,
        TRADING_SESSION_PHASES,
      );
      if (surface.account_owner !== null) {
        assertOneOf(
          `${fixtureName}.operator_surface.account_owner.phase`,
          surface.account_owner.phase,
          ACCOUNT_OWNER_PHASES,
        );
      }
      assertOneOf(
        `${fixtureName}.operator_surface.configuration.verdict`,
        surface.configuration.verdict,
        OPERATOR_VERDICTS,
      );
      assertOneOf(
        `${fixtureName}.operator_surface.current_risk.posture`,
        surface.current_risk.posture,
        RISK_POSTURES,
      );
      assertOneOf(
        `${fixtureName}.operator_surface.current_risk.verdict`,
        surface.current_risk.verdict,
        OPERATOR_VERDICTS,
      );
      assertOneOf(
        `${fixtureName}.operator_surface.action_plan.consumption`,
        surface.action_plan.consumption,
        ACTION_PLAN_CONSUMPTIONS,
      );
      assertOneOf(
        `${fixtureName}.operator_surface.action_plan.anomaly_verdict`,
        surface.action_plan.anomaly_verdict,
        OPERATOR_VERDICTS,
      );
      assertOneOf(
        `${fixtureName}.operator_surface.submit_readiness.code`,
        surface.submit_readiness.code,
        SUBMIT_READINESS_CODES,
      );
      assertOneOf(
        `${fixtureName}.operator_surface.trader_guidance.situation_code`,
        surface.trader_guidance.situation_code,
        TRADER_SITUATION_CODES,
      );
      assertRemediation(
        `${fixtureName}.operator_surface.trader_guidance.primary_remediation`,
        surface.trader_guidance.primary_remediation,
      );
      surface.trader_guidance.additional_attention_groups.forEach(
        (group, index) => {
          assertOneOf(
            `${fixtureName}.operator_surface.trader_guidance.additional_attention_groups[${index}].severity`,
            group.severity,
            TRADER_ATTENTION_SEVERITIES,
          );
          assertRemediation(
            `${fixtureName}.operator_surface.trader_guidance.additional_attention_groups[${index}].remediation`,
            group.remediation,
          );
        },
      );
      for (const [actionName, action] of Object.entries(surface.actions)) {
        assertOneOf(
          `${fixtureName}.operator_surface.actions.${actionName}.effect`,
          action.effect,
          ACTION_EFFECTS,
        );
        action.gate_results.forEach((gate, index) =>
          assertGateResult(
            `${fixtureName}.operator_surface.actions.${actionName}.gate_results[${index}]`,
            gate,
          ),
        );
      }
      surface.readiness_gates.forEach((gate, index) => {
        assertGateResult(
          `${fixtureName}.operator_surface.readiness_gates[${index}].gate_result`,
          gate.gate_result,
        );
        if (gate.suggested_action !== null) {
          assertRemediation(
            `${fixtureName}.operator_surface.readiness_gates[${index}].suggested_action`,
            gate.suggested_action,
          );
        }
      });
      assertRuntimeFreshness(
        `${fixtureName}.operator_surface.runtime_freshness`,
        surface.runtime_freshness,
      );
      if (surface.control_plane !== null) {
        assertOneOf(
          `${fixtureName}.operator_surface.control_plane.state`,
          surface.control_plane.state,
          DAEMON_RESULT_KINDS,
        );
      }
      if (surface.broker_observation_consistency !== null) {
        assertOneOf(
          `${fixtureName}.operator_surface.broker_observation_consistency.verdict`,
          surface.broker_observation_consistency.verdict,
          BROKER_OBSERVATION_VERDICTS,
        );
      }
      if (surface.reconciliation !== null) {
        assertOneOf(
          `${fixtureName}.operator_surface.reconciliation.state`,
          surface.reconciliation.state,
          RECONCILIATION_STATES,
        );
      }
      if (surface.broker_activity_health !== null) {
        assertOneOf(
          `${fixtureName}.operator_surface.broker_activity_health.state`,
          surface.broker_activity_health.state,
          BROKER_ACTIVITY_STATES,
        );
        assertNotice(
          `${fixtureName}.operator_surface.broker_activity_health.headline`,
          surface.broker_activity_health.headline,
        );
        surface.broker_activity_health.notices.forEach((notice, index) =>
          assertNotice(
            `${fixtureName}.operator_surface.broker_activity_health.notices[${index}]`,
            notice,
          ),
        );
      }
      assertNotice(
        `${fixtureName}.operator_surface.incident_headline`,
        surface.incident_headline,
      );

      assertLifecycleGraph(
        `${fixtureName}.lifecycle_chart.global_graph`,
        fixture.lifecycle_chart.global_graph,
      );
      for (const [graphId, graph] of Object.entries(
        fixture.lifecycle_chart.subgraphs,
      )) {
        assertLifecycleGraph(
          `${fixtureName}.lifecycle_chart.subgraphs.${graphId}`,
          graph,
        );
      }
      fixture.lifecycle_chart.actions.forEach((action, index) => {
        assertOneOf(
          `${fixtureName}.lifecycle_chart.actions[${index}].id`,
          action.id,
          LIFECYCLE_ACTION_IDS,
        );
        assertOneOf(
          `${fixtureName}.lifecycle_chart.actions[${index}].tone`,
          action.tone,
          LIFECYCLE_ACTION_TONES,
        );
      });
    }
  });
});
