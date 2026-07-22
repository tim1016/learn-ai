# docs/archive/

This directory holds documentation that has been superseded, completed, or determined to be ephemeral. Files here are preserved for provenance — not for active reference.

**Prune by default; archive deliberately.** The 2026-07-04 policy changed new
point-in-time docs to hard deletion in git history. The 2026-07-22 Clerk/controller
consolidation is a deliberate exception: obsolete operating material moved here because
it remains useful audit evidence, but must never be mistaken for current procedure.

## Directory layout

| Subdirectory | Contents |
|---|---|
| `handoffs/` | Session-end context dumps, demo notes, overnight progress logs — ephemeral operational records |
| `prompts/` | Verbatim LLM prompts stored as files — not design docs |
| `plans/` | Implementation plans whose work has shipped and whose authority has been absorbed into a canonical doc |
| `reports/` | One-time audit reports, phase snapshots, frozen reconciliation artifacts |
| `runbooks/` | Superseded operator and recovery runbooks |

## Status banner convention

Every archived file carries a banner at the top:

```markdown
> **Status:** Archived / superseded.
> **Do not use as implementation authority.**
> Current authority: `path/to/canonical.md`
> Archived because: one-sentence reason
```

This convention exists so AI agents loading archived files get an immediate, unambiguous signal. The banner is the minimum; the move to this directory reinforces it.

## What belongs here vs. deletion

**Policy change (2026-07-04).** Point-in-time working docs normally **hard-delete
to git history**, not archive. Git history is the provenance record; any open defects
are lifted into `docs/known-gaps.md` before the source files are deleted. Archive only
when a dated document has material operational/audit provenance that should remain
readable, and give it the status banner above plus a current-authority pointer.

- **Prune to git history** (provenance lives in git): completed implementation plans, design specs for shipped features, phase snapshots, session handoffs, LLM prompts, closed audit findings.
- **Delete outright** (no provenance value at all): raw test-runner stdout, generated artifacts with no analytical content.

Earlier raw-artifact deletions are listed in `docs/archive/deleted-artifacts.md`. The
2026-07-04 prune is recorded in `docs/doc-authority.md` (see its "2026-07-04 prune" note).

## Do not edit archived files

If the work described in an archived file needs to be revisited, open a new document — do not un-archive or edit the existing one.
