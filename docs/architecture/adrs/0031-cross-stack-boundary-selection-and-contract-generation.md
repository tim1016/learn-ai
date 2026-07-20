# ADR 0031: Cross-Stack Boundary Selection and Generated Contracts

- **Date:** 2026-07-20
- **Status:** Accepted
- **Context:** Issue #1126

## Decision

The platform has two supported browser-to-service boundary paths. A feature
chooses its path from the runtime topology; it must not add a second transport
only to make its types look uniform.

| Runtime path | Contract authority | Angular client contract | Freshness gate |
| --- | --- | --- | --- |
| Angular → GraphQL/.NET → FastAPI | Hot Chocolate schema for GraphQL; FastAPI OpenAPI for any Python-owned forwarded payload | GraphQL Code Generator's client preset from checked-in `.graphql` operations; `openapi-typescript` aliases for Python-owned inputs | Export GraphQL schema and require its snapshot to be committed; regenerate Angular outputs and require no diff |
| Angular → FastAPI | FastAPI/Pydantic OpenAPI | `openapi-typescript` generated REST types | Export OpenAPI deterministically and require its snapshot and generated output to be committed |

`contracts/graphql/backend.schema.graphql` and
`contracts/openapi/python-data-service.openapi.json` are versioned,
reviewable snapshots. They are generated artifacts, not hand-edited sources of
truth. `PythonDataService/scripts/export_openapi_contract.py` exports FastAPI
without calling external services; `dotnet run --project Backend -- schema
export` exports Hot Chocolate without running database migrations.

The only allowed handwritten frontend contract code is one of:

1. A type alias/refinement over a generated type, with its local invariant
   explained at the declaration.
2. A transport shape that OpenAPI cannot describe, currently SSE event
   payloads. It must identify its owning Pydantic model and have a boundary
   fixture or focused test.

The strategy-spec editor is the first vertical slice: its Python input types
are OpenAPI aliases, and its `runSpecStrategyBacktest` result and variables
come from a generated GraphQL operation. The direct broker data-plane health
response is likewise an OpenAPI alias. This establishes the migration pattern;
it does not claim that every historical frontend interface is already
generated.

## Rationale

GraphQL is an intentional .NET presentation boundary for browser operations
that use persisted data, GraphQL composition, or .NET-owned authorization. The
Python service remains the authority for mathematical input/output and direct
data-plane controls. Sending direct FastAPI traffic through a new .NET relay
would add a failure and ownership boundary without improving the contract.

Conversely, generating REST types from FastAPI does not describe a GraphQL
selection set, aliases, or projection such as the strategy backtest's
indicator dictionary becoming a list. The GraphQL operation is its own
contract, and must be generated from the checked-in GraphQL schema.

## Verification

CI enforces all three freshness checks:

- Python exports the OpenAPI snapshot with `--check`.
- Backend exports the GraphQL schema and fails on an uncommitted diff.
- Frontend regenerates both client outputs and fails on an uncommitted diff.

`contracts/fixtures/` carries strict shared examples for the highest-risk
boundaries. Python validates the source models, .NET deserializes the two
Python-to-.NET payloads and projects the backtest result through GraphQL, and
Angular consumes the generated direct-control type. Timestamps in every
fixture are `int64 ms UTC`.

## Consequences

- A schema or operation change cannot silently leave generated frontend code
  stale in CI.
- A direct FastAPI flow remains direct; it does not receive a gratuitous .NET
  relay.
- New GraphQL browser work adds a checked-in `.graphql` operation before use.
- New direct REST browser work imports a generated OpenAPI type rather than
  hand-copying its Pydantic fields.
- SSE and other non-OpenAPI envelopes remain explicit exceptions until a
  transport-specific generator is adopted.
