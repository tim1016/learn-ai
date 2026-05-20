import { describe, expect, it } from "vitest";
import {
  FieldNode,
  OperationDefinitionNode,
  SelectionNode,
  SelectionSetNode,
} from "graphql";
import { BACKTEST_RUNS_QUERY } from "./backtest-runs.query";

/**
 * PR B.3 regression — the canonical ``DataPolicy`` wire contract is
 * snake_case (see ``Frontend/src/app/models/data-policy.ts`` and spec
 * § 6.1), but Hot Chocolate v15 exposes the GraphQL schema as camelCase.
 * The query must declare GraphQL field aliases so the response keys
 * match the consumer-side TypeScript interface. Without these aliases,
 * ``RunHistoryComponent.barsSummary()`` dereferences
 * ``dp.input_bars.timespan`` on a response shaped ``{ inputBars: ... }``
 * and throws at runtime, breaking history rendering for every modern run.
 */

function findField(set: SelectionSetNode | undefined, name: string): FieldNode | undefined {
  if (!set) return undefined;
  return set.selections.find(
    (s: SelectionNode): s is FieldNode => s.kind === "Field" && s.name.value === name,
  );
}

function operation(): OperationDefinitionNode {
  const def = BACKTEST_RUNS_QUERY.definitions.find(
    (d): d is OperationDefinitionNode => d.kind === "OperationDefinition",
  );
  if (!def) throw new Error("BACKTEST_RUNS_QUERY has no operation definition");
  return def;
}

function dataPolicySelection(): SelectionSetNode {
  const op = operation();
  const backtestRuns = findField(op.selectionSet, "backtestRuns");
  const nodes = findField(backtestRuns?.selectionSet, "nodes");
  const dp = findField(nodes?.selectionSet, "dataPolicy");
  if (!dp?.selectionSet) {
    throw new Error("dataPolicy selection set not found in BACKTEST_RUNS_QUERY");
  }
  return dp.selectionSet;
}

describe("BACKTEST_RUNS_QUERY — DataPolicy snake_case alias contract (PR B.3)", () => {
  it.each<[string, string]>([
    ["inputBars", "input_bars"],
    ["strategyBars", "strategy_bars"],
    ["timestampPolicy", "timestamp_policy"],
    ["providerKind", "provider_kind"],
    ["fixtureId", "fixture_id"],
    ["fixtureSha256", "fixture_sha256"],
  ])("aliases server '%s' to client '%s'", (serverName, clientAlias) => {
    const field = findField(dataPolicySelection(), serverName);
    expect(field, `dataPolicy.${serverName} field selection missing`).toBeDefined();
    expect(field?.alias?.value, `dataPolicy.${serverName} must be aliased to ${clientAlias}`)
      .toBe(clientAlias);
  });

  it.each(["source", "symbol", "adjusted", "session", "timezone"])(
    "leaves '%s' un-aliased (already snake_case-compatible)",
    (name) => {
      const field = findField(dataPolicySelection(), name);
      expect(field).toBeDefined();
      expect(field?.alias).toBeUndefined();
    },
  );
});
