/* Faithful reproduction of the CURRENT /data-lab-docs RSI accordion panel,
   expanded. All layout matches the real SCSS + HTML 1:1 so the annotation
   dots sit on real pixels. */

const MockBefore = () => (
  <div className="mock-before">
    <div className="data-lab-docs">

      {/* one panel, expanded */}
      <div className="panel" id="ind-rsi">
        <div className="ah">
          <i className="chevron">▼</i>
          <div className="feature-header">
            <span className="feature-name">Relative Strength Index (RSI)</span>
            <span className="feature-badge">1 COL</span>
            <span className="caveat-chip" title="Has important data caveats"><i>⚠</i></span>
          </div>
        </div>

        <div className="ac">

          {/* Quick Info card */}
          <div className="quick-info-card">
            <div className="quick-info-header">
              <span>⚡</span> Quick Info
            </div>
            <div className="quick-info-body">
              <div className="qi-row">
                <span className="qi-label">Why use it</span>
                <span className="qi-value">See if a stock is "Overstretched" or exhausted.</span>
              </div>
              <div className="qi-row analogy-row">
                <span className="qi-label">Think of it as</span>
                <span className="qi-value qi-analogy">
                  Pulling a rubber band to the "70" mark — very tight, wants to snap back (Overbought). At "30," stretched the other way (Oversold).
                </span>
              </div>
              <div className="qi-row">
                <span className="qi-label">Trading impact</span>
                <span className="qi-value">Tells you when it's "Dangerous to Buy" because everyone else already bought.</span>
              </div>
              <div className="qi-row qi-check">
                <span className="qi-label">Test yourself</span>
                <span className="qi-value">
                  <span className="qi-question">If RSI is at 85, is it a good time to start a long-term investment?</span>
                  <span className="qi-answer">Probably not — wait for a dip</span>
                </span>
              </div>
            </div>
            <div className="quick-info-footer">
              <span className="qi-ref"><span>🔖</span> J. Welles Wilder Jr. (1978)</span>
              <span className="qi-delay"><span>🕑</span> 15-minute delay is a major risk; RSI might show "35" while real price has already hit "20" and bounced.</span>
            </div>
          </div>

          {/* Technical Deep Dive */}
          <div className="indicator-detail">
            <div className="subsection-title"><span>%</span> Formula</div>
            <div className="formula-block">
              <span className="big">
                RSI = 100 − <span className="frac"><span>100</span><span>1 + <span className="frac"><span>AvgGain(n)</span><span>AvgLoss(n)</span></span></span></span>
              </span>
            </div>

            <div className="subsection-title"><span>ⓘ</span> Description</div>
            <p className="interpretation">
              Measures speed and magnitude of price changes. Uses Wilder's RMA smoothing by default. Values above 70 = overbought, below 30 = oversold.
            </p>

            <div className="subsection-title"><span>💡</span> Interpretation</div>
            <ul className="interpretation-list">
              <li>RSI &gt; 70 → overbought (potential reversal or strong uptrend)</li>
              <li>RSI &lt; 30 → oversold (potential reversal or strong downtrend)</li>
              <li>RSI divergence from price signals weakening momentum</li>
              <li>Centerline (50) crossover used as trend filter</li>
            </ul>

            <div className="subsection-title"><span>🕑</span> Recommended Timeframes</div>
            <p className="interpretation">5m–1D (excellent on all common timeframes)</p>

            <div className="subsection-title"><span>⚙</span> Default Parameters</div>
            <p className="interpretation"><code>length = 14</code></p>

            <div className="subsection-title"><span>⊞</span> Output Columns</div>
            <div className="column-chips">
              <code className="column-chip">rsi_&#123;n&#125;</code>
            </div>

            <div className="subsection-title"><span>⚠</span> Data Notes</div>
            <ul className="data-notes">
              <li>Requires <code>length</code> warmup bars (Wilder smoothing)</li>
            </ul>

            <div className="subsection-title"><span>🔗</span> Related Indicators</div>
            <div className="related-chips">
              <button className="related-link">Stochastic RSI</button>
              <button className="related-link">Money Flow Index (MFI)</button>
              <button className="related-link">Commodity Channel Index (CCI)</button>
            </div>

            <div className="subsection-title"><span>📦</span> Library</div>
            <p className="interpretation"><code>pandas-ta (ta.rsi, mamode="rma")</code></p>
          </div>

        </div>
      </div>
    </div>
  </div>
);

window.MockBefore = MockBefore;
