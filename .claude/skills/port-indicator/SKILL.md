---
name: port-indicator
description: Port an indicator, signal, or strategy from a reference implementation (LEAN, open-source backtester, paper, or prior experiment) into PythonDataService with strict numerical equivalence. Use when user says "port", "reimplement from", "copy from LEAN", "translate this indicator", "bring in this strategy from", or references an external repo/paper they want math from.
---

# Port Indicator

Port a mathematical construct (indicator, signal rule, strategy, fill model) from a reference source into `PythonDataService/` with strict numerical equivalence. The port must be testable, cited, and sovereign — meaning the reference dependency is eliminated after porting.

## When to use

- User asks to port code from LEAN, QuantConnect, a GitHub repo, or a paper
- User references a reference implementation they want reproduced in this engine
- User says "match what <reference> does for <indicator>"

## When NOT to use

- If the task is building something new with no reference — this is just regular Python work
- If the task is reconciling an existing port that diverges — use `reconcile-backtest` instead
- If the task is extracting math from a paper only — use `extract-math-from-paper` first, then this skill to build the port

## Execution

Execute these phases in strict order. Do not skip Phase 1 even if the reference looks simple.

### PHASE 1: Identify the canonical source

Before writing a single line of port code, pin down exactly what is being ported.

1. **Locate the reference.** Ask the user for one of: (a) a file path in `references/`, (b) a GitHub URL with commit SHA, (c) a PDF in `references/papers/` with section and equation numbers, (d) pasted code with attribution.
2. **Pin the version.** If it's a GitHub URL without a commit SHA, use the GitHub MCP to resolve the current HEAD SHA and record it. A port against `main` is worthless six months from now.
3. **Read the whole thing.** Read not just the indicator function but its callers, its state initialization, its warmup handling, and its unit tests if they exist. Mathematical bugs hide in initialization.
4. **Identify dependencies in the reference.** Does the reference use a utility (rolling window, exponential smoother, bar aggregator) that we don't have yet? These get ported too, as prerequisites. List them before starting.

### PHASE 2: Build the golden fixture

A golden fixture is a deterministic input → output record derived from the reference itself. It is the ground truth our port must reproduce.

1. **Generate input.** Use either (a) real historical data from Postgres via the Postgres MCP, or (b) a synthetic sequence the user specifies (e.g., step function, sine wave, random walk with known seed).
2. **Run the reference against the input.** If the reference is C# (LEAN), run it via `dotnet script` or the user's LEAN setup. If it's Python, run it directly. If it's a paper, hand-compute the first 5–10 values using the equations.
3. **Serialize the output.** Store the golden fixture as JSON or Parquet in `PythonDataService/tests/fixtures/golden/<indicator-name>/`. Include: input data, reference version (commit SHA or paper section), exact output values with full float precision, any state the reference exposed.
4. **Never regenerate the golden fixture without a reason.** If it needs to change, the change gets its own commit with a justification.

### PHASE 3: Write the port

1. **Match naming to the reference** where reasonable. If LEAN calls it `ExponentialMovingAverage`, our port is `ExponentialMovingAverage` or `exponential_moving_average` (following Python conventions), not a reinvented name. Variable names inside the function should match the paper/reference notation exactly — if the paper uses `alpha`, don't rename to `smoothing_factor`.
2. **Match initialization and warmup exactly.** If the reference starts producing output at bar N, ours starts at bar N. If it uses the first value as seed, so do we. Warmup bugs are the most common source of divergence.
3. **Preserve timestamp alignment.** If the reference computes on bar close, we compute on bar close. If it uses exchange time, we use exchange time. Document the convention in the module docstring.
4. **No silent type coercion.** Use explicit `dtype` in pandas. Use `numpy.float64` unless the reference uses a different precision.
5. **Document the port** in the module docstring: what was ported, source (with commit SHA or paper ref), tolerance used, date of port.

### PHASE 4: Prove equivalence

1. **Write a test in `PythonDataService/tests/unit/`** that loads the golden fixture and asserts the port reproduces it within tolerance.
2. **Default tolerance: `atol=1e-9, rtol=0`** for indicator values. Tighter if the reference is integer or exact rational; looser only with justification documented in the test.
3. **Test edge cases:** empty input, single-value input, NaN in input, warmup region, mid-series discontinuity if the reference handles them.
4. **If the test fails, do not relax the tolerance.** Use the `reconcile-backtest` skill's classification taxonomy to diagnose:
   - `timestamp` — bar alignment or clock difference
   - `warmup` — initialization or seeding difference
   - `fill` — order fill assumption mismatch (for strategies)
   - `commission` — commission model mismatch (for strategies)
   - `precision` — floating-point accumulation (this is where `rtol` might be justified)
   - `off-by-one` — window boundary or index slip
   - `data-quality` — the inputs themselves differ
5. **Fix the port, not the test.** The test is ground truth.

### PHASE 5: Document and eliminate the dependency

1. **Create `docs/references/<indicator-name>.md`** with: what was ported, exact source (path or URL + commit), why this reference (over alternatives), tolerance used, any known divergences and why they were accepted.
2. **Update the module docstring** to cite `docs/references/<indicator-name>.md`.
3. **If the reference was vendored for this port only**, ask the user whether to keep it in `references/` (for future audit) or remove it. Default: keep. Disk is cheap; reproducibility is not.

## Output

After completing a port, report back to the user:

- What was ported, from where (with SHA or paper ref)
- Tolerance used, and the max absolute error observed against the golden fixture
- Any divergences found during reconciliation and how they were resolved
- Files created: port module, test, fixture, `docs/references/` note
- Next steps: how to expose via FastAPI if that's the user's goal (delegate to `add-fastapi-endpoint`)

## Anti-patterns to avoid

- "It looks right" — no equivalence proof is not acceptable
- Relaxing tolerance to make a test pass
- Porting without pinning the reference version
- Silent `np.allclose` with default tolerance — always specify `atol` and `rtol` explicitly
- Renaming variables for "clarity" that breaks traceability to the source
- Skipping warmup tests because "it converges after a few bars"
