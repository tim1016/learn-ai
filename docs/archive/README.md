# docs/archive/

This directory holds documentation that has been superseded, completed, or determined to be ephemeral. Files here are preserved for provenance — not for active reference.

## Directory layout

| Subdirectory | Contents |
|---|---|
| `handoffs/` | Session-end context dumps, demo notes, overnight progress logs — ephemeral operational records |
| `prompts/` | Verbatim LLM prompts stored as files — not design docs |
| `plans/` | Implementation plans whose work has shipped and whose authority has been absorbed into a canonical doc |
| `reports/` | One-time audit reports, phase snapshots, frozen reconciliation artifacts |

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

- **Archive** (preserve provenance): plans, design docs, phase specs, research notes, session handoffs, LLM prompts
- **Delete** (no provenance value): raw test runner stdout, generated artifacts with no analytical content

Deleted raw artifacts are listed in `docs/archive/deleted-artifacts.md`.

## Do not edit archived files

If the work described in an archived file needs to be revisited, open a new document — do not un-archive or edit the existing one.
