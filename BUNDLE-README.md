# learn-ai AI scaffolding bundle

This bundle is the AI scaffolding for the learn-ai repo. Unpack into `C:\Users\inkan\learn-ai\` and follow the setup steps below.

## What's in this bundle

```
learn-ai/
├── CLAUDE.md                       # Main orientation (~150 lines)
├── .claude/
│   ├── settings.json               # MCP servers + permission scoping
│   ├── skills/                     # 8 lazy-loaded skills
│   │   ├── port-indicator/SKILL.md
│   │   ├── reconcile-backtest/SKILL.md
│   │   ├── extract-math-from-paper/SKILL.md
│   │   ├── trading-domain/SKILL.md
│   │   ├── add-fastapi-endpoint/SKILL.md
│   │   ├── write-graphql-resolver/SKILL.md
│   │   ├── build-angular-component/SKILL.md
│   │   └── meta-propose-skill/SKILL.md
│   └── rules/                      # 5 stack rule files, referenced from CLAUDE.md
│       ├── angular.md
│       ├── dotnet.md
│       ├── python.md
│       ├── testing.md
│       └── numerical-rigor.md
├── docs/
│   └── references/                 # (empty placeholder) Per-port attribution notes
└── references/                     # (empty placeholder) Vendored reference code
```

## Setup steps

### 1. Back up your existing CLAUDE.md

```powershell
# From C:\Users\inkan\learn-ai\
Rename-Item CLAUDE.md CLAUDE.md.old
```

### 2. Unpack this bundle

Unzip into the repo root, merging with existing files. The only file it replaces is `CLAUDE.md` (which you just backed up).

### 3. Fill in credentials

The `.claude/settings.json` uses environment variables. Set these in your shell profile or a local `.env` file (do NOT commit `.env`):

- `POLYGON_API_KEY` — your Polygon.io API key
- `POSTGRES_READONLY_URL` — a read-only Postgres connection string, e.g. `postgresql://readonly_user:password@localhost:5432/learn_ai`
- `GITHUB_TOKEN` — a GitHub personal access token with `repo` scope

**Before granting the Postgres MCP access, create a read-only role in Postgres:**

```sql
CREATE ROLE readonly_user WITH LOGIN PASSWORD 'your_password';
GRANT CONNECT ON DATABASE learn_ai TO readonly_user;
GRANT USAGE ON SCHEMA public TO readonly_user;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO readonly_user;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO readonly_user;
```

Defense in depth: the MCP is configured read-only, and the DB role is read-only too.

### 4. Verify skills are discovered

From the repo root, open Claude Code and run:

```
/skills
```

You should see 8 skills listed (the ones in `.claude/skills/`), plus any personal skills from `~/.claude/skills/` including your `/trade` plugin.

### 5. Smoke test

Try a task that should trigger a skill and observe:

- **"Port the SMA indicator from LEAN into our Python engine"** → should trigger `port-indicator`
- **"Why does my backtest not match LEAN for SPY 2024?"** → should trigger `reconcile-backtest`
- **"Add a GraphQL resolver for fetching EMA values"** → should trigger `write-graphql-resolver`
- **"Build a chart component that shows an EMA line"** → should trigger `build-angular-component`

If a skill doesn't trigger when it should, the `description` field's trigger phrases need tuning. Edit the skill's frontmatter and retry.

## Philosophy quick reference

1. **Math rigor before stack hygiene.** Your primary job is porting math from reference sources with strict numerical equivalence.
2. **Numerical claims require receipts.** Every port ships with a golden fixture, a tolerance-pinned test, and a `docs/references/` note.
3. **Sovereignty over the math.** Reference code is studied and ported, then the dependency is eliminated.
4. **Skills are lazy.** Only name + description lives in context; bodies load on demand. Keep descriptions specific, keep bodies under ~500 lines.
5. **Rules are reference.** CLAUDE.md points to `.claude/rules/` files; they're read when relevant, not all the time.

## Iterating

After two weeks of use:

- **Run `meta-propose-skill`** mentally against your recent sessions. Any task you did three times that doesn't have a skill? Propose one.
- **Check skill activation.** If Claude isn't triggering a skill when you expect, the `description` needs better trigger phrases matching how you actually talk.
- **Prune unused skills.** If a skill hasn't triggered in a month and you don't miss it, delete it. Skills have a context cost.
- **Grow `docs/references/`.** Every port adds a file. This becomes your repo's porting history.
- **Update rules** when official docs change. Rules files should cite their authoritative source (already done in the headers); when angular.dev updates, your `.claude/rules/angular.md` might need a pass.

## What's NOT in this bundle (deliberately)

- **The `/trade` plugin.** It's a personal plugin for stock analysis, different job from learn-ai. Kept separate.
- **Subagent definitions.** Your current recurring tasks are sequential, not parallel. Subagents are a tool for later if a task genuinely needs parallel fan-out (e.g., "port this to three languages and reconcile all three").
- **Hooks.** Claude Code supports lifecycle hooks (PreToolUse, PostToolUse, etc.) but nothing in this bundle needs them yet. Consider a PreToolUse hook later to auto-run `ruff` before commits.
- **Custom commands.** Skills cover the recurring tasks; commands would overlap. Add commands only if you want a specific terse invocation (`/port` vs letting `port-indicator` auto-trigger).

## Acknowledging gaps

A few things this bundle doesn't fully solve, that you may want to address later:

- **`trading-domain` is a first draft.** Its glossary reflects what I inferred from our conversation — expect to edit it as you notice things I got wrong or conventions I missed.
- **`references/` is empty.** On your first port, you'll need to decide whether to vendor via git submodule or copy. I'd lean submodule for LEAN (live repo, you'll want updates) and copy for abandoned repos or snippets.
- **No CI integration.** The rules and tests here work in local dev; wiring `ruff`, `eslint`, `dotnet format`, and the pytest/Vitest/xUnit runners into CI is a separate task.
- **No pre-commit hooks.** Worth adding once the ruleset is stable.
