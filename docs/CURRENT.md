# Current Documentation Entry Point

Use this file when you need the current docs map without loading every plan,
audit, and handoff in the repository.

## Start Here

1. `AGENTS.md` — cross-agent operating rules and repo philosophy.
2. `CLAUDE.md` — Claude Code-specific project instructions.
3. `docs/doc-authority.md` — authoritative map of canonical, supporting, and archived docs.

## Core Authority Docs

- `docs/math-sources-of-truth.md` — canonical implementation per math concept.
- `docs/architecture/engine-authority-map.md` — which engine owns each job.
- `docs/architecture/numerical-authority-migration-plan.md` — active math-authority migration sequence.
- `.claude/rules/numerical-rigor.md` — tolerances, golden fixtures, timestamp rules, reconciliation taxonomy.
- `docs/known-gaps.md` — living open-defect backlog: what is still broken or deferred.
- `docs/bot-control-operator-manual.md` — comprehensive operator manual for the bot control system + Account Clerk: the three planes, lifecycle, every admission gate + reason code, freezes/recovery, and the operational blindspots.

## Current Cleanup Notes

- Agent-facing rules and skills live under `.claude/`, not `.Codex/`.
- Generated and runtime-heavy paths are hidden from normal `rg` searches by `.rgignore`.
- Historical plans and audit findings are useful provenance, but implementation authority should flow through `docs/doc-authority.md`.
- 2026-07-04: ~150 point-in-time docs (completed plans, session handoffs, shipped-feature PRDs, closed audit findings) were pruned to git history; open defects were consolidated into `docs/known-gaps.md`, and `docs/doc-authority.md` gained an ADR index. New point-in-time docs are pruned to git history, not archived.
