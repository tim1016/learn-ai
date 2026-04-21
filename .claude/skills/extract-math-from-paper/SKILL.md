---
name: extract-math-from-paper
description: Transcribe mathematical equations from a PDF paper into testable Python code with paper-section citations. Use when user says "implement from this paper", "code up this equation", "extract the math from", "translate this formula", or uploads a PDF with academic/quantitative content.
---

# Extract Math From Paper

Transcribe equations from a PDF paper (academic, quantitative finance, or technical) into Python modules with docstring citations back to paper sections and equation numbers. This is typically the first step before `port-indicator` when the reference is a paper rather than code.

## When to use

- User uploads a PDF and asks to implement the math
- User references "equation 3.2 in the paper" or similar
- User wants to code up a formula from a textbook or published method

## When NOT to use

- The reference is code (GitHub, LEAN) — use `port-indicator` directly
- The user just wants a summary of the paper — this skill writes code, not summaries

## Execution

### PHASE 1: Read the paper properly

Use the `pdf-reading` skill to read the PDF. Do not skim.

1. **Read the full methodology section**, not just the equation. Equations in isolation are often under-specified; the surrounding prose defines variable domains, assumptions, edge cases, and conventions.
2. **Note the variable nomenclature section** if one exists. Papers almost always define variables in a glossary or at first use — respect those names.
3. **Identify the input/output contract.** What does the equation take? What does it produce? What are the units? What are the domains and ranges?
4. **Identify assumptions.** "Assume log-normal returns", "assume continuous compounding", "assume no dividends" — these must be preserved in the port or explicitly violated with documentation.

### PHASE 2: Transcribe, do not interpret

When writing the Python, preserve the paper's notation.

1. **Variable names match paper notation exactly.** If the paper uses `S_t` for spot price at time t, the Python variable is `S_t` or `s_t`, not `spot_price`. Preserve Greek letters via transliteration: `sigma`, `mu`, `theta`, etc.
2. **Equation structure matches paper layout.** If the paper writes it as a sum from i=1 to N, write it as a sum from i=1 to N. Do not "optimize" into a vectorized form until after the literal transcription has tests passing.
3. **Docstring every function with the paper citation.** Format:
   ```python
   def black_scholes_call(S: float, K: float, T: float, r: float, sigma: float) -> float:
       """European call price per Black-Scholes (1973).

       Ref: Black, F. & Scholes, M. (1973). Section 2, Equation 13.
       Notation follows the paper:
           S: current stock price
           K: strike price
           T: time to expiration (years)
           r: risk-free rate (continuous)
           sigma: volatility (annualized)

       Assumes: no dividends, European exercise, lognormal returns,
       continuous hedging, constant risk-free rate and volatility.
       """
   ```
4. **Flag ambiguities as you transcribe.** If the paper is unclear about whether a sum is inclusive or exclusive of a boundary, write a comment `# AMBIGUITY: paper does not specify whether sum is inclusive of N. Assuming inclusive.` and tell the user.

### PHASE 3: Unit and boundary tests

Papers usually state boundary conditions even when they don't state tests. Extract them.

1. **Extract any numerical examples the paper gives.** Most methodology papers include a worked example with specific numbers. Those become unit tests with exact expected values.
2. **Test the stated boundary behavior.** If the paper says "as T → 0, the price approaches max(S-K, 0)", write that test. If the paper says "price is monotonically increasing in sigma", write a property-based test with several sigmas.
3. **Test the assumptions are enforced** where reasonable. If the function requires T > 0, it should raise on T = 0 or T < 0 (or return a documented sentinel).
4. **Store the paper itself in `references/papers/`** with a meaningful filename: `black-scholes-1973.pdf`, not `paper.pdf`. Reference that path from the docstring.

### PHASE 4: Document for the next port

Even if the next step is immediate use (not a further port), leave notes for future re-derivation.

1. **Create `docs/references/<method-name>.md`** with: paper citation (full), section and equation numbers used, any ambiguities and how they were resolved, any deviations from the paper and why.
2. **Note what's NOT implemented.** If the paper describes a generalized method and we implemented the constant-sigma special case, say so. Future work will want to know.

## Output

Report to the user:

- Function(s) written, with signatures
- Paper sections and equation numbers covered
- Ambiguities found and resolutions chosen (request user confirmation on each)
- Tests written (paper's worked examples, boundary behavior, stated properties)
- Any deviations from the paper and why

## Anti-patterns to avoid

- "Paraphrasing" variable names into English words (breaks traceability)
- Vectorizing or optimizing before the literal transcription has tests passing
- Silent assumption of convention (e.g., business days vs calendar days) when the paper doesn't specify — flag it
- Omitting the docstring citation because "it's obvious from context"
- Skipping the paper's worked example because "it'll obviously work" — that's the whole point of having it

## Copyright note

Do not reproduce large verbatim excerpts from the paper in docstrings or comments. Cite, paraphrase briefly, and reference the section number. The paper itself goes in `references/papers/` for access; code does not need to reproduce it.
