/* ═══════════════════════════════════════════════════════════
   Main review document for /data-lab-docs#ind-rsi
   ═══════════════════════════════════════════════════════════ */

const { useState, useEffect } = React;

/* ── Small helpers ─────────────────────────────────── */
const Sev = ({ level, children }) => <span className={`sev ${level}`}>{children}</span>;

const Finding = ({ sev, title, children }) => (
  <div className="finding">
    <Sev level={sev}>{sev === "win" ? "keep" : sev}</Sev>
    <div className="body">
      <h4>{title}</h4>
      {children}
    </div>
  </div>
);

const Code = ({ children, className = "" }) => (
  <pre className={`code ${className}`}>{children}</pre>
);

const FixLabel = ({ children }) => <div className="fix-label">{children}</div>;

/* ── Annotated before screenshot ──────────────────── */
const AnnotatedBefore = () => {
  /* dot positions are a % of container — tuned by eye against mock-before layout */
  const dots = [
    { n: 1, top: "1.5%",  left: "41%", note: "Double title: accordion header already says 'Relative Strength Index (RSI)'. Opening it reveals the same info repackaged three ways.", heading: "Redundant naming" },
    { n: 2, top: "7%",    left: "7%",  note: "'Quick Info' card dominates the fold before the actual definition. User lands on /ind-rsi and sees a rubber-band analogy before they see a formula or parameters.", heading: "Buried spec" },
    { n: 3, top: "24%",   left: "6%",  note: "Footer mixes a citation with a 200-character delay warning. Two unrelated facts crammed on one row; the warning gets lost.", heading: "Footer collision" },
    { n: 4, top: "31%",   left: "6%",  note: "Every subsection gets its own emoji/icon. 8 icons in a single card — each is just a coloured bullet in amber, creating visual noise that reads as all-warning.", heading: "Icon soup" },
    { n: 5, top: "38%",   left: "50%", note: "Formula rendered center-aligned with a single KaTeX line but the rest of the card is left-aligned. Creates an odd visual axis shift.", heading: "Inconsistent alignment" },
    { n: 6, top: "59%",   left: "6%",  note: "'Recommended Timeframes', 'Default Parameters', 'Output Columns', 'Library' are all terse one-liners rendered as full H3 sections. They should be a compact spec table.", heading: "Over-sectioned metadata" },
    { n: 7, top: "75%",   left: "7%",  note: "The Data Notes block shouts: full amber background, amber left-border, amber text. One bullet that says 'needs warmup bars' shouldn't look like a compliance warning.", heading: "False urgency" },
    { n: 8, top: "86%",   left: "7%",  note: "'Related' uses button elements styled as green chips — keyboard users tab through three buttons with no role/label; screen readers announce them as unlabeled buttons.", heading: "A11y: chip buttons" },
  ];

  return (
    <div>
      <div className="annotated" style={{position: "relative"}}>
        <MockBefore />
        {dots.map(d => (
          <span key={d.n} className="annot-dot" style={{top: d.top, left: d.left}} title={`${d.heading}: ${d.note}`}>
            {d.n}
          </span>
        ))}
      </div>
      <ul className="annot-list">
        {dots.map(d => (
          <li key={d.n}>
            <span className="annot-num">{d.n}</span>
            <div><strong>{d.heading}.</strong> {d.note}</div>
          </li>
        ))}
      </ul>
    </div>
  );
};

/* ── Compare ────────────────────────────────────── */
const BeforeAfter = () => {
  const [which, setWhich] = useState("after");
  return (
    <div>
      <div className="tabs">
        <button className={which === "before" ? "active" : ""} onClick={() => setWhich("before")}>Current</button>
        <button className={which === "after"  ? "active" : ""} onClick={() => setWhich("after")}>Proposed</button>
        <button className={which === "split"  ? "active" : ""} onClick={() => setWhich("split")}>Side by side</button>
      </div>

      {which === "before" && <div className="tab-panel active"><MockBefore /></div>}
      {which === "after"  && <div className="tab-panel active"><MockAfter /></div>}
      {which === "split"  && (
        <div className="tab-panel active compare">
          <div className="compare-col before">
            <span className="col-label">Current</span>
            <div style={{transform: "scale(0.78)", transformOrigin: "top left", width: "128%"}}>
              <MockBefore />
            </div>
          </div>
          <div className="compare-col after">
            <span className="col-label">Proposed</span>
            <div style={{transform: "scale(0.78)", transformOrigin: "top left", width: "128%"}}>
              <MockAfter />
            </div>
          </div>
        </div>
      )}
    </div>
  );
};

/* ── Sidebar ────────────────────────────────────── */
const Sidebar = () => {
  const items = [
    { group: "Overview" },
    { id: "summary",  label: "Summary" },
    { id: "context",  label: "What I reviewed" },

    { group: "Code quality" },
    { id: "cq-data",     label: "1.1 Monolithic data in component" },
    { id: "cq-dup",      label: "1.2 Duplicated template blocks" },
    { id: "cq-scroll",   label: "1.3 Unsafe anchor scroll" },
    { id: "cq-types",    label: "1.4 Types & constants" },
    { id: "cq-a11y",     label: "1.5 Accessibility" },
    { id: "cq-perf",     label: "1.6 Rendering & perf" },

    { group: "UI / UX" },
    { id: "ux-annot",    label: "2.1 Annotated review" },
    { id: "ux-card",     label: "2.2 Redesigned card" },
    { id: "ux-page",     label: "2.3 Page-level redesign" },

    { group: "Plan" },
    { id: "plan",        label: "Rollout" },
  ];
  return (
    <aside className="sidebar">
      <div className="sidebar-brand">Design review</div>
      <div className="sidebar-title">data-lab-docs<br/><span style={{color:"var(--text-muted)", fontSize:"var(--fs-sm)", fontWeight:400}}>#ind-rsi</span></div>
      <ul className="toc">
        {items.map((it, i) =>
          it.group
            ? <li key={"g"+i} className="toc-group">{it.group}</li>
            : <li key={it.id}><a href={`#${it.id}`}>{it.label}</a></li>
        )}
      </ul>
    </aside>
  );
};

/* ── Page ───────────────────────────────────────── */
const Review = () => (
  <div className="shell">
    <Sidebar />
    <main className="main">

      <header className="page-head">
        <div className="eyebrow-row">
          <span className="dot" />
          Frontend · Angular 17 · Primeng 20 · data-lab-docs.component.ts
        </div>
        <h1>Code &amp; UX review: <span style={{color:"var(--text-secondary)"}}>/data-lab-docs#ind-rsi</span></h1>
        <p className="lede">
          The page teaches 30+ technical indicators. It works, the content is strong, and
          the dark styling is on-brand. It also suffers from a monolithic component,
          repeated templates, and a density/tone mismatch: the "quick info" layer fights
          the reference-manual purpose. This document proposes concrete fixes for both.
        </p>
        <div className="page-meta">
          <div className="meta-item"><span>Files reviewed</span><strong>3</strong></div>
          <div className="meta-item"><span>LoC</span><strong>~1,950</strong></div>
          <div className="meta-item"><span>Indicators rendered</span><strong>31</strong></div>
          <div className="meta-item"><span>Open PR effort</span><strong>~2 days</strong></div>
        </div>
      </header>

      {/* ── Summary ─────────────────────────────── */}
      <section id="summary" className="sec">
        <h2><span className="num">00</span> Summary</h2>
        <div className="summary-grid">
          <div className="summary-card high"><div className="label">High</div><div className="value">2</div><div className="desc">Monolithic data in component · duplicated template</div></div>
          <div className="summary-card med"><div className="label">Med</div><div className="value">5</div><div className="desc">Unsafe scroll · a11y · typing · false-urgency styling · over-sectioning</div></div>
          <div className="summary-card low"><div className="label">Low</div><div className="value">4</div><div className="desc">Icon usage · alignment · chip semantics · copy tone</div></div>
          <div className="summary-card nit"><div className="label">Wins</div><div className="value">3</div><div className="desc">Good: KaTeX directive, tracked <code>@for</code>, strong content authorship</div></div>
        </div>
      </section>

      <section id="context" className="sec">
        <h2><span className="num">01</span> What I reviewed</h2>
        <p className="sec-sub">
          The public route <code>/data-lab-docs</code> in the Frontend Angular app, which renders
          a scroll-through reference of every indicator the Data Lab pipeline produces. Specifically:
        </p>
        <div style={{display:"flex", gap:"var(--space-2)", flexWrap:"wrap", marginTop:"var(--space-2)"}}>
          <span className="chip file">data-lab-docs.component.ts</span>
          <span className="chip file">data-lab-docs.component.html</span>
          <span className="chip file">data-lab-docs.component.scss</span>
          <span className="chip">app.routes.ts</span>
        </div>
      </section>

      {/* ══════════════════════════════════════════
          CODE QUALITY
          ══════════════════════════════════════════ */}

      <section id="cq-data" className="sec">
        <div className="sec-head">
          <h2><span className="num">1.1</span> Monolithic data in component class</h2>
          <span className="count">high · ~1,500 LoC of data in a <code>.component.ts</code></span>
        </div>
        <p className="sec-sub">
          <code>allIndicators</code> is a 31-item array of ~60-line objects hard-coded in the
          component file, alongside <code>csvBaseColumns</code>, <code>dataCaveats</code>, and{" "}
          <code>validationNotes</code>. That's content, not logic, and it makes the component hard
          to review, translate, test, or move to a backend.
        </p>

        <Finding sev="high" title="Extract to a typed data module, then a content source">
          <p>
            Move the array to <code>indicator-docs.data.ts</code>. Immediate wins: file becomes
            navigable, HMR is faster, the component fits on one screen, and you can add a test that
            validates every record has a formula + warmup note + analogy without loading the
            component. Medium-term, this should live as JSON in the backend alongside the pandas-ta
            metadata and ship in the metadata JSON itself — so the docs page and the download
            agree by construction.
          </p>

          <FixLabel>Proposed structure</FixLabel>
          <Code>
{`src/app/components/data-lab/data-lab-docs/
├─ data-lab-docs.component.ts      // ~80 lines: presentation + computed signals
├─ data-lab-docs.component.html
├─ data-lab-docs.component.scss
└─ data/
   ├─ indicator-doc.types.ts       // IndicatorDoc, PanelType, Caveat
   ├─ indicators.data.ts           // const INDICATORS: readonly IndicatorDoc[]
   ├─ caveats.data.ts
   └─ csv-base-columns.data.ts`}
          </Code>

          <FixLabel>Thin component after</FixLabel>
          <Code>
{`import { INDICATORS } from './data/indicators.data';

@Component({ ... })
export class DataLabDocsComponent {
  readonly overlay = INDICATORS.filter(i => i.panelType === 'overlay');
  readonly subPanel = INDICATORS.filter(i => i.panelType === 'sub-panel');

  readonly byName = new Map(INDICATORS.map(i => [i.name, i]));
  getDisplayName = (id: string) => this.byName.get(id)?.displayName ?? id;
}`}
          </Code>
        </Finding>

        <Finding sev="low" title="Derive overlay / sub-panel lists instead of re-filtering in the template">
          <p>
            The template calls <code>overlayIndicators</code> and <code>subPanelIndicators</code>;
            if those are getters that re-filter, they run on every change detection. Make them
            either <code>readonly</code> fields computed in the constructor or{" "}
            <code>computed()</code> signals.
          </p>
        </Finding>
      </section>

      <section id="cq-dup" className="sec">
        <div className="sec-head">
          <h2><span className="num">1.2</span> Duplicated accordion template</h2>
          <span className="count">high · 2 identical 90-line blocks</span>
        </div>
        <p className="sec-sub">
          The HTML has two <code>&lt;p-accordion&gt;</code> blocks (overlay + sub-panel), and each
          contains the full quick-info + technical-detail markup verbatim. Any future change to the
          card layout has to be made twice.
        </p>

        <Finding sev="high" title="Extract a single <app-indicator-card> component">
          <p>
            Make an <code>@Input() indicator: IndicatorDoc</code> component that owns the card's
            entire markup. The parent template becomes one <code>@for</code> loop per section.
          </p>

          <FixLabel>Before (both sections, identical internals)</FixLabel>
          <Code>{`<p-accordion [multiple]="true">
  @for (ind of overlayIndicators; track ind.name) {
    <p-accordionpanel [attr.id]="'ind-' + ind.name">
      <p-accordionheader> …quick-info… …detail… </p-accordionheader>
      <p-accordioncontent>
        <!-- 90 lines duplicated verbatim in the sub-panel loop -->
      </p-accordioncontent>
    </p-accordionpanel>
  }
</p-accordion>`}</Code>

          <FixLabel>After</FixLabel>
          <Code>{`<p-accordion [multiple]="true">
  @for (ind of overlayIndicators; track ind.name) {
    <app-indicator-card [indicator]="ind" />
  }
</p-accordion>
<p-accordion [multiple]="true">
  @for (ind of subPanelIndicators; track ind.name) {
    <app-indicator-card [indicator]="ind" />
  }
</p-accordion>`}</Code>

          <p>
            Bonus: the extracted component is the natural home for the tabbed layout proposed in §2.2,
            with internal <code>signal()</code>s for the active tab per card.
          </p>
        </Finding>
      </section>

      <section id="cq-scroll" className="sec">
        <div className="sec-head">
          <h2><span className="num">1.3</span> Fragile anchor scroll</h2>
          <span className="count">med · uses <code>inject(ElementRef)</code> + implicit DOM IDs</span>
        </div>
        <p className="sec-sub">
          <code>scrollToIndicator(name)</code> presumably does{" "}
          <code>el.nativeElement.querySelector('#ind-' + name).scrollIntoView()</code>. That has
          three problems: (1) <code>scrollIntoView</code> can hijack the whole page inside an app
          shell, (2) it silently no-ops if the target is collapsed, and (3) it doesn't update the URL.
        </p>

        <Finding sev="med" title="Use Router fragment + CSS scroll-margin-top">
          <p>
            Expand-if-needed, then navigate with the router fragment. The browser does the scroll
            and the URL becomes shareable.
          </p>
          <Code>{`// data-lab-docs.component.ts
private readonly router = inject(Router);

goToIndicator(name: string) {
  this.expanded.add(name);                // ensure the panel is open
  this.router.navigate([], {
    fragment: 'ind-' + name,
    replaceUrl: true,
  });
}

// CSS (once, in styles.scss)
[id^='ind-'] { scroll-margin-top: 72px; } // under sticky header`}</Code>
        </Finding>
      </section>

      <section id="cq-types" className="sec">
        <div className="sec-head">
          <h2><span className="num">1.4</span> Types &amp; constants</h2>
          <span className="count">med · lots of stringly-typed fields</span>
        </div>

        <Finding sev="med" title="Tighten IndicatorDoc">
          <p>Several fields that should be structured are free-form strings.</p>
          <Code>{`// Today
interface IndicatorDoc {
  defaultParams: string;           // 'length = 14'  (free-form)
  recommendedTimeframes: string;   // '5m–1D (excellent on all common timeframes)'
  library: string;                 // 'pandas-ta (ta.rsi, mamode="rma")'
  panelType: 'overlay' | 'sub-panel';
  // …
}

// Proposed
interface IndicatorDoc {
  defaults: Array<{ name: string; value: string | number; note?: string }>;
  timeframes: { best: Timeframe[]; supported: Timeframe[]; notes?: string };
  library: { pkg: 'pandas-ta'; call: string; extraArgs?: Record<string, unknown> };
  panel: 'overlay' | 'sub-panel';
  tags: Array<'trend' | 'momentum' | 'volatility' | 'volume' | 'ma'>;
  caveats: Array<{ severity: 'info' | 'warn' | 'risk'; body: string }>;
  // …
}`}</Code>
          <p>
            Structured fields unlock the page-level filters in §2.3 (tag chips, timeframe filter),
            the severity-coloured caveat rows in §2.2, and the spec-grid table.
          </p>
        </Finding>

        <Finding sev="low" title="Inline LaTeX strings are hard to validate">
          <p>
            The formula strings (escaped <code>\\text</code>, <code>\\sum</code>, …) are brittle —
            one wrong backslash silently KaTeX-errors. Add a unit test that loads every indicator
            and renders its formula through <code>katex.renderToString</code>, failing on any
            parse error.
          </p>
        </Finding>

        <Finding sev="nit" title="Field naming drift">
          <p>
            <code>quickWhy</code>, <code>quickAnalogy</code>, <code>quickImpact</code>,{" "}
            <code>quickWhy</code>, <code>checkQuestion</code>, <code>checkAnswer</code>,{" "}
            <code>professionalRef</code>, <code>timeframeBehavior</code> mix two
            sub-structures. Group them: <code>teaching: {'{ why, analogy, impact, check?: { q, a } }'}</code>{" "}
            and <code>attribution: {'{ firstPublishedBy, year, source? }'}</code>.
          </p>
        </Finding>
      </section>

      <section id="cq-a11y" className="sec">
        <div className="sec-head">
          <h2><span className="num">1.5</span> Accessibility</h2>
          <span className="count">med · screen readers + keyboard</span>
        </div>

        <Finding sev="med" title="Icons carry meaning with no text alternative">
          <p>
            The amber <code>&lt;i class="pi pi-exclamation-triangle"&gt;</code> in the accordion
            header conveys "has caveats" visually; it has a <code>title</code> attribute but no{" "}
            <code>aria-label</code>. Screen readers will read only the indicator name. Add:
          </p>
          <Code>{`<span class="caveat-chip" role="img" aria-label="Has data caveats">
  <i class="pi pi-exclamation-triangle" aria-hidden="true"></i>
</span>`}</Code>
        </Finding>

        <Finding sev="med" title="Related-indicator chips are buttons but don't look like them">
          <p>
            <code>.related-link</code> is a <code>&lt;button&gt;</code> styled as a green pill. It
            takes focus — good — but lacks a visible focus ring at PrimeNG's reset. Add:
          </p>
          <Code>{`.related-link:focus-visible {
  outline: 2px solid $accent;
  outline-offset: 2px;
}`}</Code>
          <p>
            Also replace the on-click <code>scrollToIndicator()</code> with a real anchor so{" "}
            Cmd-click opens the target in a new tab and the URL can be shared.
          </p>
        </Finding>

        <Finding sev="low" title="Formula block has no accessible description">
          <p>
            KaTeX renders a purely visual equation. Pass a plain-English fallback through{" "}
            <code>aria-label</code> on the wrapper (e.g. "RSI equals 100 minus 100 divided by 1
            plus the ratio of average gain to average loss over n periods").
          </p>
        </Finding>

        <Finding sev="low" title="Color-only state in the Check question">
          <p>
            The Q/A relies on red → green colour change alone ("Q:" amber → "A:" green). Also
            reveal on click/keypress and prefix the "A:" with an icon or the word "Answer" so
            colour-blind users get it.
          </p>
        </Finding>
      </section>

      <section id="cq-perf" className="sec">
        <div className="sec-head">
          <h2><span className="num">1.6</span> Rendering &amp; perf</h2>
          <span className="count">low</span>
        </div>

        <Finding sev="win" title="Good: OnPush + @for with track">
          <p>
            The component already uses <code>ChangeDetectionStrategy.OnPush</code> and every
            template <code>@for</code> uses a <code>track</code>. Keep that.
          </p>
        </Finding>

        <Finding sev="low" title="All 31 accordions render their full body up front">
          <p>
            PrimeNG's <code>&lt;p-accordionpanel&gt;</code> renders children eagerly, so all 31
            formulas are KaTeX-rendered on first paint. Lazy-render the body on expansion:
          </p>
          <Code>{`<p-accordionpanel #p [attr.id]="'ind-' + ind.name">
  <p-accordionheader>…</p-accordionheader>
  <p-accordioncontent>
    @if (p.expanded()) {
      <app-indicator-card [indicator]="ind" />
    }
  </p-accordioncontent>
</p-accordionpanel>`}</Code>
          <p>Should cut first-paint KaTeX cost by ~95% on this route.</p>
        </Finding>

        <Finding sev="nit" title="KatexDirective inputs">
          <p>
            <code>[appKatex]</code> + <code>[displayMode]</code> as separate inputs is fine. Minor:
            make <code>displayMode</code> default to <code>true</code> in this context and drop it
            from every call site.
          </p>
        </Finding>
      </section>

      {/* ══════════════════════════════════════════
          UI / UX
          ══════════════════════════════════════════ */}

      <section id="ux-annot" className="sec">
        <div className="sec-head">
          <h2><span className="num">2.1</span> What I see on the RSI card</h2>
          <span className="count">annotated · dot = issue</span>
        </div>
        <p className="sec-sub">
          Below is a pixel-accurate reproduction of the expanded RSI panel (built from the real
          SCSS). Hover each dot for the specific problem; full list below the image.
        </p>
        <AnnotatedBefore />
      </section>

      <section id="ux-card" className="sec">
        <div className="sec-head">
          <h2><span className="num">2.2</span> Proposed card redesign</h2>
          <span className="count">before / after · interactive</span>
        </div>

        <p className="sec-sub">
          Same content, reorganised around the reader's three real jobs when they land on{" "}
          <code>#ind-rsi</code>:
        </p>
        <ul style={{fontSize:"var(--fs-sm)", color:"var(--text-secondary)", marginTop:0, paddingLeft: "1.25rem"}}>
          <li><strong>Explain</strong> — "what is this and why would I use it" (the analogy + self-check live here)</li>
          <li><strong>Math &amp; spec</strong> — formula, library call, params, columns, warmup (the old monolith, tightened to a dl/dt/dd table)</li>
          <li><strong>How to use it</strong> — interpretation rules as a signal table, crosslinks, severity-leveled caveats</li>
        </ul>
        <p style={{fontSize:"var(--fs-sm)", color:"var(--text-secondary)"}}>
          Critical meta (panel type, column count, warmup risk, delay risk, best timeframes, params)
          is always visible in the header + spec strip, so a user searching for "does RSI need warmup
          bars" gets it without opening a tab.
        </p>

        <BeforeAfter />

        <h3 style={{marginTop:"var(--space-6)"}}>Design rationale (8 changes, in priority order)</h3>

        <Finding sev="win" title="1. Lead with the spec, not the analogy">
          <p>
            This is a <em>reference page</em>. Users come from the Data Lab form, where they just
            checked a box for RSI and want to know "what does this add to my CSV." Put library,
            params, columns, timeframes in a 4-column strip that's always visible. The rubber-band
            analogy is still there, but behind the "Explain" tab where it belongs.
          </p>
        </Finding>

        <Finding sev="win" title="2. Collapse 8 subsection headers into 3 tabs">
          <p>
            The current card has 8 h3-equivalent headings (Formula, Description, Interpretation,
            Timeframes, Parameters, Columns, Data Notes, Related, Library). That's a table of
            contents pretending to be a card. Tabs let a user scan one thing at a time, and short
            tabs mean no scrolling inside the card.
          </p>
        </Finding>

        <Finding sev="win" title="3. Severity-leveled caveats">
          <p>
            Today every data note is "amber block on amber background." The 15-minute-delay warning
            reads the same as "needs 14 warmup bars." Split the caveats into three severities with
            distinct colour treatments:
          </p>
          <ul style={{fontSize:"var(--fs-sm)", color:"var(--text-secondary)", paddingLeft:"1.25rem"}}>
            <li><span style={{color:"var(--bear)", fontWeight:600}}>Risk</span> — actively changes how you trade (feed delay, session boundary reset for VWAP)</li>
            <li><span style={{color:"var(--warn)", fontWeight:600}}>Warn</span> — caveats you must understand but not alarms (warmup bars, NaN handling)</li>
            <li><span style={{color:"var(--accent)", fontWeight:600}}>Info</span> — parameter defaults, TradingView-compat flags</li>
          </ul>
        </Finding>

        <Finding sev="win" title="4. Replace bullet interpretation with a signal table">
          <p>
            "RSI &gt; 70 → overbought" is a signal-value pair. Render it as a signal-value pair:
            left column is the condition in a badge, right column is the interpretation. Scannable;
            compare across indicators.
          </p>
        </Finding>

        <Finding sev="win" title="5. Analogy stops being a full-width callout">
          <p>
            The italic + accent-blue + left-border treatment for the analogy screams at the user.
            Demote it to a sibling card of the same shape as Why / Impact. The content stays; the
            visual volume drops.
          </p>
        </Finding>

        <Finding sev="win" title="6. Self-check becomes real interaction">
          <p>
            Today's "Q: … / A: …" just prints both. Make the answer click-to-reveal so the user
            actually engages. Costs 10 lines of signal code; turns passive reading into a quiz.
          </p>
        </Finding>

        <Finding sev="win" title="7. Replace icon soup with hierarchy">
          <p>
            Drop the amber icon before every subsection title. It communicates nothing and turns
            the card into a "page of warnings." The old Quick Info lightning bolt goes too — the
            tab label already says "Explain."
          </p>
        </Finding>

        <Finding sev="win" title="8. Related indicators as prose, not chips">
          <p>
            "Pairs well with <em>Stochastic RSI</em> (faster oscillator on the same input),{" "}
            <em>MFI</em> (volume-weighted cousin), …" communicates relationship. Three green pills
            named "Stochastic RSI", "Money Flow Index (MFI)", "Commodity Channel Index (CCI)" do
            not. Keeps the crosslink click target; adds semantic context.
          </p>
        </Finding>
      </section>

      <section id="ux-page" className="sec">
        <div className="sec-head">
          <h2><span className="num">2.3</span> Page-level redesign</h2>
          <span className="count">the real killer problem</span>
        </div>
        <p className="sec-sub">
          Zoom out: the page is a 30-indicator vertical scroll with no search, no filter, and no
          persistent navigation. If you hit <code>#ind-rsi</code> from a link, fine — but anyone
          browsing the catalog has to scroll or use the browser's in-page find. Here's a
          catalog-style two-pane layout with sticky left rail, search, and panel filters:
        </p>
        <PageRedesign />
        <h3 style={{marginTop:"var(--space-6)"}}>Why the two-pane wins here</h3>
        <Finding sev="win" title="Sticky navigation with tag filters">
          <p>
            The left rail shows all 31 indicators with a short code, full name, and two severity
            dots. Searching "volume" filters to OBV, MFI, CMF, AD, VWAP. The right pane is the{" "}
            §2.2 redesigned card, so nothing is lost from the current page — only the scroll pain.
          </p>
        </Finding>
        <Finding sev="win" title="Deep links still work and get richer">
          <p>
            <code>#ind-rsi</code> opens the RSI card (same behavior). Add a second hash segment
            for the active tab: <code>#ind-rsi/math</code> jumps to the formula.{" "}
            <code>#ind-rsi/use</code> jumps to interpretation rules. Great for citing in runbooks.
          </p>
        </Finding>
        <Finding sev="win" title="Keyboard-first flow">
          <p>
            <kbd>/</kbd> focuses search. <kbd>↑</kbd>/<kbd>↓</kbd> moves through the list. <kbd>Enter</kbd>{" "}
            loads into the right pane. <kbd>1</kbd>/<kbd>2</kbd>/<kbd>3</kbd> switches tab. Researchers
            cruising the catalog will use this constantly.
          </p>
        </Finding>
      </section>

      {/* ══════════════════════════════════════════
          PLAN
          ══════════════════════════════════════════ */}

      <section id="plan" className="sec">
        <div className="sec-head">
          <h2><span className="num">03</span> Rollout plan</h2>
          <span className="count">~2 days, 4 PRs</span>
        </div>

        <Finding sev="low" title="PR 1 — Extract data (2 hrs, zero visual change)">
          <p>
            Move <code>allIndicators</code> / <code>dataCaveats</code> / <code>csvBaseColumns</code>{" "}
            into <code>data/</code>. Component drops from ~1,950 LoC to ~80. Ship behind no flag;
            it's pure refactor.
          </p>
        </Finding>

        <Finding sev="low" title="PR 2 — Extract <app-indicator-card> (3 hrs)">
          <p>
            One component, two <code>@for</code> loops. Ships identical UI. Sets up PR 3.
          </p>
        </Finding>

        <Finding sev="low" title="PR 3 — Redesign the card (1 day)">
          <p>
            Replace card internals with the tabbed layout. Tighten types (
            <code>caveats: {'{ severity, body }[]'}</code>, <code>tags</code>, etc.). Flag behind{" "}
            <code>featureDocsV2</code> for internal review.
          </p>
        </Finding>

        <Finding sev="low" title="PR 4 — Two-pane page (0.5 day)">
          <p>
            Add sticky left rail with search + filters. Keep the current single-scroll as{" "}
            <code>/data-lab-docs?v=scroll</code> for a release, then retire.
          </p>
        </Finding>

        <p style={{fontSize:"var(--fs-sm)", color:"var(--text-secondary)", marginTop:"var(--space-5)"}}>
          None of the four PRs changes the <em>content</em> — the indicator data, formulas, analogies,
          caveats, and citations in <code>allIndicators</code> are already good. The redesign is all
          information architecture and styling.
        </p>
      </section>

    </main>
  </div>
);

ReactDOM.createRoot(document.getElementById("root")).render(<Review />);
