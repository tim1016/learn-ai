# `references/`

Vendored reference source code, frozen at specific commits, used as **ground truth for what we are porting**. Per `AGENTS.md`, this is authority rank 1 (above official docs and `.claude/rules/*.md`).

## Layout

```text
references/
└── <project>/
    └── <commit-sha>/
        ├── attribution.md   # what was vendored, when, why, license
        └── ...source tree...
```

## Rules

- **Frozen.** Code here is not edited. If the upstream changes, we vendor a new commit-SHA subdirectory; we do not modify in place.
- **Cited.** Every port note in `docs/references/` cites a `references/<project>/<sha>/...` path or a paper section.
- **Audit-only.** This code does not run in production. It exists to be read, compared against, and used for one-time fixture generation.
- **License-aware.** Every vendored subtree carries its `attribution.md` documenting the upstream license and a SPDX identifier. If a subtree is under a license that prohibits redistribution, we vendor a *minimized legally-safe extract* (only the function bodies under audit, not the whole repo) and note the omission.

## Currently vendored

| Project | Commit | Vendored on | License | Path |
|---|---|---|---|---|
| QuantConnect/Lean | `7986ed0aade3ae5de06121682409f05984e32ff7` | 2026-04-26 | Apache-2.0 | `lean/7986ed0aade3ae5de06121682409f05984e32ff7/` |

## Adding a new vendored reference

1. Identify the exact upstream commit (or paper version) you are porting from.
2. Create `references/<project>/<commit-sha>/` (or `references/<source-name>/<version>/` for papers).
3. Copy only the files actually being ported, plus their immediate dependencies. Do not bulk-copy the upstream repo.
4. Write `attribution.md` with: source URL, commit/version, vendoring date, license + SPDX ID, list of files vendored, link to the matching `docs/references/<construct-name>.md` note.
5. Update this README's "Currently vendored" table.

## Why this directory exists

Memory and hyperlinks are not enough. A parity test that says "matches LEAN" is meaningless six months from now if upstream has rewritten the indicator and the commit it was matched against is gone from anyone's checkout. Vendoring freezes the comparison target in our own version control.

See `.claude/rules/numerical-rigor.md` § Sovereignty for the full reasoning.
