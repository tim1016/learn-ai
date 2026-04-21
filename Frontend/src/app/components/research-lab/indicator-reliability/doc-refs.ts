/**
 * Central registry of tooltip content + methodology-doc anchors for every
 * metric shown on the Indicator Reliability page. Each entry pairs:
 *   - a short definition rendered in the hover tooltip,
 *   - a `section` slug (without `#`) that deep-links into the full
 *     methodology doc via the side drawer.
 *
 * Keys are camelCase metric IDs. Anchors track the auto-generated heading
 * IDs in `docs/indicator-reliability-methodology.md` (see the slugify in
 * `MarkdownViewerComponent`).
 *
 * Convention: keep definitions under ~160 characters so the tooltip fits
 * in a small popover without wrapping into a paragraph.
 */

export interface DocRef {
  /** Short title (bold first line of the tooltip). */
  title: string;
  /** One- or two-sentence definition for hover. */
  definition: string;
  /** Section anchor in the methodology doc (no leading `#`). */
  section: string;
}

export const DOC_REFS = {
  // ── Hero / verdict ─────────────────────────────────────────────
  confidenceScore: {
    title: 'Confidence score',
    definition:
      'Zero to 100. Twenty points each for FDR significance, Bonferroni significance, OOS holding (retention delta ≥ −30% or positive), |z vs random| > 3, and |IC| > 0.10 (10 partial).',
    section: '532-confidence-score',
  },
  verdictBucket: {
    title: 'Verdict bucket',
    definition:
      'Score ≥ 85 → TRADE. 60 ≤ score < 85 → INVESTIGATE. Below 60 → REJECT. Colour-coded gauge ring and headline follow the same scheme.',
    section: '532-confidence-score',
  },
  directionLabel: {
    title: 'Direction',
    definition:
      'Sign of IC at best horizon. Negative → mean-reversion, positive → momentum, near-zero → none. Thresholded at |IC| > 0.02.',
    section: '37-verdict-labels',
  },
  strengthLabel: {
    title: 'Strength',
    definition:
      'Bucket on |IC|. Strong ≥ 0.12, Moderate 0.07–0.12, Weak 0.03–0.07, Noise < 0.03. Tuned for time-series daily IC on liquid equities.',
    section: '37-verdict-labels',
  },
  stabilityLabel: {
    title: 'Stability',
    definition:
      'Bucket on hit-rate (fraction of daily ICs whose sign matches the aggregate sign). High ≥ 0.58, Moderate 0.52–0.58, Low < 0.52.',
    section: '36-stability-metrics',
  },
  tradeabilityLabel: {
    title: 'Tradeability (proxy)',
    definition:
      'From |Sharpe proxy|. ≥ 1.0 with High stability → Likely tradeable. ≥ 0.5 → Marginal. Else → Unlikely. Heavily caveated — see §3.12.',
    section: '312-ir-proxy-and-tradeability',
  },
  heroOosIc: {
    title: 'Out-of-sample IC',
    definition:
      'Mean daily IC on the held-out 30% test set. Primary signal that the in-sample edge is not overfit.',
    section: '31-daily-information-coefficient',
  },
  heroOosVsIs: {
    title: 'OOS vs IS delta',
    definition:
      '(|OOS IC| / |IS IC| − 1) × 100. Positive = OOS stronger than IS, negative = degradation. Does not detect sign flips.',
    section: '38-oos-retention-delta',
  },
  heroZvsRandom: {
    title: 'Z-score vs random',
    definition:
      '(actual IC − shuffled IC mean) / shuffled IC std, across 100 random-index shuffles. |z| > 3 → "distinguishable from noise".',
    section: '35-random-shuffle-baseline',
  },

  // ── Reason pills ────────────────────────────────────────────────
  pillFdr: {
    title: 'FDR correction',
    definition:
      'Benjamini–Hochberg adjustment for testing multiple horizons simultaneously. Monotonic; a value is always ≥ the raw NW p-value.',
    section: '34-multiple-testing-correction',
  },
  pillBonferroni: {
    title: 'Bonferroni correction',
    definition:
      'Most conservative multiple-testing adjustment: p_i × m. Rejects null only when evidence survives the harshest bound.',
    section: '34-multiple-testing-correction',
  },
  pillOosHolds: {
    title: 'OOS holds',
    definition:
      'Retention delta ≥ −30% (or positive). A quick-glance test that the edge did not collapse out-of-sample.',
    section: '38-oos-retention-delta',
  },
  pillIcMagnitude: {
    title: '|IC| threshold',
    definition:
      'A rough economic-meaning bar. |IC| > 0.10 is strong for daily time-series IC on liquid equities; below is weak.',
    section: '37-verdict-labels',
  },
  pillRegimeEdge: {
    title: 'Regime edge',
    definition:
      'Signal is materially stronger in one volatility regime than the other at the best horizon. See WHERE cell for detail.',
    section: '311-volatility-regime-conditioning',
  },
  pillSingleAsset: {
    title: 'Single-asset disclaimer',
    definition:
      'All statistics computed on a single ticker. Portfolio IC across many names may differ substantially — never extrapolate directly.',
    section: '314-honesty-footnotes',
  },

  // ── WHEN / WHERE / HOW ─────────────────────────────────────────
  whenCell: {
    title: 'When to trade',
    definition:
      'Horizon with the strongest OOS (fallback: FDR) signal. Detail is read from the decay curve — shows whether |IC| fades quickly or decays slowly past the peak.',
    section: '535-when-cell',
  },
  whereCell: {
    title: 'Where it works',
    definition:
      'Regime with the stronger |IC| at best horizon. Volatility split is IS median of rolling 20-bar realized vol — research-grade, not point-in-time.',
    section: '536-where-cell',
  },
  howCell: {
    title: 'How to enter',
    definition:
      'Direction-based rule (Fade extremes / Follow the move / No clear edge) plus the Sharpe proxy. Always test with costs before sizing.',
    section: '537-how-cell',
  },

  // ── Content grid / panels ──────────────────────────────────────
  decayCurvePanel: {
    title: 'IC decay curve',
    definition:
      'IC at every integer horizon 1..N on the in-sample period, with ±1.96·SE band. Visualisation only; no multiple-testing correction.',
    section: '310-ic-decay-curve',
  },
  horizonCardIc: {
    title: 'Per-horizon IC',
    definition:
      'In-sample mean daily IC for this horizon. Colour-coded: green if positive, red if negative.',
    section: '31-daily-information-coefficient',
  },
  horizonCardNwT: {
    title: 'NW t-statistic',
    definition:
      'Newey–West HAC-corrected t-stat. Uses a Bartlett kernel with Andrews (1991) bandwidth to account for serial correlation in daily ICs.',
    section: '32-neweywest-hac-corrected-statistics',
  },
  horizonCardZ: {
    title: 'σ from random',
    definition:
      'Z-score of this horizon\'s IC versus the 100-sim random-shuffle baseline. |σ| > 3 treated as distinguishable from noise.',
    section: '35-random-shuffle-baseline',
  },
  bestHorizonTag: {
    title: 'Best horizon',
    definition:
      'Selected by OOS significance first (OOS p < 0.10), with FDR p < 0.10 as fallback. Drives the verdict, WHEN, and HOW cells.',
    section: '37-verdict-labels',
  },

  regimePanel: {
    title: 'Volatility regime crosscheck',
    definition:
      'IC re-computed on high-vol (above rolling-vol median) and low-vol (at or below) subsets of the training set. Forward returns computed before masking so "horizon" still means h bars in real time.',
    section: '311-volatility-regime-conditioning',
  },
  regimeRowHigh: {
    title: 'High-vol regime',
    definition:
      'Bars where rolling 20-bar realized volatility exceeded the in-sample median. Requires ≥ 50 bars to be reported.',
    section: '311-volatility-regime-conditioning',
  },
  regimeRowLow: {
    title: 'Low-vol regime',
    definition:
      'Bars where rolling 20-bar realized volatility was at or below the in-sample median. Requires ≥ 50 bars.',
    section: '311-volatility-regime-conditioning',
  },
  hitRate: {
    title: 'Hit rate',
    definition:
      'Fraction of daily ICs whose sign matches the aggregate IC sign. Measures directional consistency, not just "positive days".',
    section: '36-stability-metrics',
  },

  // ── Right column ──────────────────────────────────────────────
  checklistPanel: {
    title: 'Decision checklist',
    definition:
      'Five pass/fail tests that must all clear to trade. Looser thresholds than the confidence gauge (so the checklist reads "still holds" while the gauge docks points).',
    section: '538-five-test-decision-checklist',
  },
  checklistFdr: {
    title: 'FDR significance',
    definition:
      'At least one horizon cleared FDR-adjusted p < 0.05 after correcting for the number of horizons tested.',
    section: '34-multiple-testing-correction',
  },
  checklistBonferroni: {
    title: 'Bonferroni (conservative)',
    definition:
      'At least one horizon cleared the strictest correction: p × m < 0.05.',
    section: '34-multiple-testing-correction',
  },
  checklistOosHolds: {
    title: 'Out-of-sample holds',
    definition:
      'Retention delta ≥ −40%. Looser than the gauge\'s −30% cutoff on purpose — the checklist answers "not obviously broken", the gauge answers "fully confident".',
    section: '538-five-test-decision-checklist',
  },
  checklistBeatsRandom: {
    title: 'Beats random',
    definition:
      '|z vs random| > 3. The actual IC is more than 3σ from the random-shuffle baseline mean.',
    section: '35-random-shuffle-baseline',
  },
  checklistEconomicallyMeaningful: {
    title: 'Economically meaningful',
    definition:
      '|IC| > 0.10 at the best horizon. Statistical power plus a plausible magnitude.',
    section: '37-verdict-labels',
  },

  noiseFloorPanel: {
    title: 'Random baseline',
    definition:
      'Random-shuffle null: 100 permutations of the indicator. Establishes the noise floor; your IC is plotted relative to it.',
    section: '35-random-shuffle-baseline',
  },
  noiseFloorBar: {
    title: 'Noise-floor bar',
    definition:
      'Horizontal axis spans ±4σ around the random IC mean. Grey band = ±1σ of the random distribution. Coloured marker = your IC.',
    section: '539-noise-floor-bar',
  },

  dailyIcPanel: {
    title: 'Daily IC over time',
    definition:
      'Raw daily IC values (light) plus a 20-bar rolling mean (bold). Shows whether the edge is stable over the IS period.',
    section: '31-daily-information-coefficient',
  },

  // ── Below the grid ─────────────────────────────────────────────
  nextStepsPanel: {
    title: 'Suggested next steps',
    definition:
      'Rule-based suggestions ranked by ordering: missing OOS → regime-dependent → stability-limited → slope variant → tradeable. Cap of four items.',
    section: '313-next-steps-rule-engine',
  },

  slopePanel: {
    title: 'Slope variant',
    definition:
      'IC of Δf_t (indicator\'s one-bar change) vs forward return. "Adds value" iff materially stronger AND more significant than raw. "Use this variant" requires OOS validation.',
    section: '39-slope-decision-flags',
  },

  methodologyFooter: {
    title: 'Methodology',
    definition:
      'Full derivations, thresholds, and caveats for every metric on this page. Opens the reference doc in a drawer.',
    section: '1-context-and-scope',
  },

  infoFootnotes: {
    title: 'Honesty footnotes',
    definition:
      'Always-on reminders of the feature\'s scope (single-asset IC, time-series not cross-sectional, overlapping returns). Muted on purpose — not alarms.',
    section: '314-honesty-footnotes',
  },

  // ── Sharpe proxy (if surfaced in a table) ─────────────────────
  sharpeProxy: {
    title: 'Sharpe proxy',
    definition:
      'IC × √(bars_per_year / horizon) under unit-vol, zero-cost, independent-bets assumptions. Rough upper bound on tradeable Sharpe, not a backtest result.',
    section: '312-ir-proxy-and-tradeability',
  },
} as const satisfies Record<string, DocRef>;

export type DocRefKey = keyof typeof DOC_REFS;
