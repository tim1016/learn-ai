/* Proposed redesign of the RSI card.
   Key moves vs. current:
   - Header row with title, one-line tagline, and all meta-tags upfront
   - 4-column always-visible spec strip (Library / Params / Columns / Timeframes)
   - 3-tab body: Explain (why + analogy + impact + self-check),
                 Math (formula + full spec table),
                 Use it (interpretation rules + related)
   - Severity-leveled caveats (info / warn / risk) instead of one amber block
   - No "icon soup" subsection titles — clean section heads only
   - Ticker-style short code next to title: RSI */

const MockAfter = () => {
  const [tab, setTab] = React.useState("explain");
  const [revealed, setRevealed] = React.useState(false);

  return (
    <div className="mock-after">
      <article className="after-card" id="ind-rsi-after">

        {/* ── Header ─────────────────────────────────────── */}
        <div className="ac-head">
          <div className="ac-title-wrap">
            <div className="ac-eyebrow">
              <span className="dot" /> Sub-panel indicator · Momentum
            </div>
            <h2 className="ac-title">
              Relative Strength Index
              <span className="ticker">RSI</span>
            </h2>
            <p className="ac-tagline">
              Measures the speed and magnitude of recent price changes on a 0–100 scale.
              Classic overbought / oversold oscillator, baseline for divergence analysis.
            </p>
          </div>

          <div className="ac-metatags">
            <span className="mt panel">Sub-panel</span>
            <span className="mt cols">1 column</span>
            <span className="mt caveat" title="Has data caveats">⚠ Warmup</span>
            <span className="mt delay" title="Affected by 15-min feed delay">⚠ 15-min delay</span>
          </div>
        </div>

        {/* ── Always-visible spec strip ──────────────────── */}
        <div className="spec-strip">
          <div className="spec">
            <span className="sl">Library</span>
            <span className="sv">pandas-ta · ta.rsi</span>
          </div>
          <div className="spec">
            <span className="sl">Default</span>
            <span className="sv">length = 14</span>
          </div>
          <div className="spec">
            <span className="sl">Output</span>
            <span className="sv">rsi_&#123;n&#125;</span>
          </div>
          <div className="spec">
            <span className="sl">Best on</span>
            <span className="sv">5m – 1D</span>
          </div>
        </div>

        {/* ── Tab strip ──────────────────────────────────── */}
        <div className="ac-tabs" role="tablist">
          <button className={tab === "explain" ? "active" : ""} onClick={() => setTab("explain")}>
            Explain
          </button>
          <button className={tab === "math" ? "active" : ""} onClick={() => setTab("math")}>
            Math &amp; spec
          </button>
          <button className={tab === "use" ? "active" : ""} onClick={() => setTab("use")}>
            How to use it
          </button>
        </div>

        {/* ── Body ───────────────────────────────────────── */}
        <div className="ac-body">

          {/* Explain */}
          <div className={"panel-content" + (tab === "explain" ? " active" : "")}>
            <div className="explain-grid">
              <div className="ex-card why">
                <div className="ex-lbl"><span className="tiny-dot" /> Why use it</div>
                <div className="ex-body">
                  See if a stock is overstretched or exhausted — is everyone already bought in, or is there still room to run?
                </div>
              </div>

              <div className="ex-card impact">
                <div className="ex-lbl"><span className="tiny-dot" /> Trading impact</div>
                <div className="ex-body">
                  Flags when buying is crowded. Above 70, the trade is "late"; below 30, fear is likely overdone.
                </div>
              </div>

              <div className="ex-card analogy">
                <div className="ex-lbl"><span className="tiny-dot" /> Think of it as</div>
                <div className="ex-body">
                  A rubber band. At 70 it's stretched tight and wants to snap back; at 30 it's stretched the other way.
                </div>
              </div>

              <div className="ex-card check">
                <div className="ex-lbl"><span className="tiny-dot" /> Check yourself</div>
                <div className="q-row">
                  If RSI is at 85, is it a good time to start a long-term investment?
                </div>
                <button
                  className={"reveal" + (revealed ? " revealed" : "")}
                  onClick={() => setRevealed(true)}>
                  {revealed ? "Probably not — wait for a dip." : "Click to reveal answer →"}
                </button>
              </div>
            </div>

            <div className="attrib">
              <div><strong>Origin</strong><br/>J. Welles Wilder Jr., 1978</div>
              <div><strong>Published in</strong><br/>New Concepts in Technical Trading Systems</div>
              <div><strong>Uses smoothing</strong><br/>Wilder's RMA (see <a href="#ind-rma-after">RMA</a>)</div>
            </div>
          </div>

          {/* Math */}
          <div className={"panel-content" + (tab === "math" ? " active" : "")}>
            <div className="math-formula">
              RSI = 100 −{" "}
              <span className="frac">
                <span>100</span>
                <span>1 + <span className="frac"><span>AvgGain(n)</span><span>AvgLoss(n)</span></span></span>
              </span>
            </div>

            <dl className="spec-grid">
              <dt>Library</dt>
              <dd><code>pandas-ta (ta.rsi, mamode="rma")</code></dd>

              <dt>Parameters</dt>
              <dd><code>length = 14</code> <span style={{color:"var(--text-muted)"}}>(lookback window)</span></dd>

              <dt>Output columns</dt>
              <dd><div className="col-list"><code>rsi_&#123;n&#125;</code></div></dd>

              <dt>Warmup</dt>
              <dd>
                <code>length</code> bars before valid values.
                Wilder smoothing means the first ~3× length bars still carry initialization bias.
              </dd>

              <dt>Range</dt>
              <dd>0 – 100 (bounded)</dd>

              <dt>Timeframes</dt>
              <dd>5m – 1D; strongest on 1h and 4h.</dd>
            </dl>
          </div>

          {/* Use it */}
          <div className={"panel-content" + (tab === "use" ? " active" : "")}>
            <ul className="interp-list">
              <li>
                <span className="sig warn">RSI &gt; 70</span>
                <span>Overbought — potential reversal, or confirmation of strong uptrend.</span>
              </li>
              <li>
                <span className="sig warn">RSI &lt; 30</span>
                <span>Oversold — potential reversal, or confirmation of strong downtrend.</span>
              </li>
              <li>
                <span className="sig bear">Divergence</span>
                <span>Price makes new high but RSI doesn't → weakening momentum.</span>
              </li>
              <li>
                <span className="sig bull">Cross 50</span>
                <span>Centerline crossover used as a simple trend filter.</span>
              </li>
            </ul>

            <div className="related-block">
              <strong>Related indicators</strong>
              Pairs well with <a href="#ind-stochrsi-after">Stochastic RSI</a> (faster oscillator on the same input),{" "}
              <a href="#ind-mfi-after">MFI</a> (volume-weighted cousin), and{" "}
              <a href="#ind-cci-after">CCI</a> (different normalisation of the same idea).
            </div>

            <div className="caveats">
              <h4>Data caveats that change how you read RSI</h4>

              <div className="caveat-row">
                <span className="cv-sev risk">Risk</span>
                <span>
                  <strong>15-minute feed delay.</strong> Displayed RSI is always 15 minutes behind the tape — RSI at 35 on screen may already have hit 20 and bounced in the real market. Do not use for real-time scalping.
                </span>
              </div>

              <div className="caveat-row">
                <span className="cv-sev warn">Warmup</span>
                <span>
                  <strong>Needs <code>length</code> bars to settle.</strong> First bars are NaN and the next ~2× length carry Wilder-smoothing initialization bias.
                </span>
              </div>

              <div className="caveat-row">
                <span className="cv-sev info">Info</span>
                <span>
                  <strong>Default is 14.</strong> Wilder's original, matches TradingView and most broker platforms out of the box.
                </span>
              </div>
            </div>
          </div>

        </div>
      </article>
    </div>
  );
};

window.MockAfter = MockAfter;
