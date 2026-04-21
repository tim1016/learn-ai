import {
  ChangeDetectionStrategy,
  Component,
  DestroyRef,
  ElementRef,
  effect,
  inject,
  input,
  signal,
  viewChild,
} from '@angular/core';
import { CommonModule } from '@angular/common';
import { HttpClient } from '@angular/common/http';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of } from 'rxjs';
import { Marked } from 'marked';
import DOMPurify from 'dompurify';
import katex from 'katex';

/**
 * Renders a markdown file from a URL with:
 *   - GitHub-style heading anchors (`1.` → `#1-...`),
 *   - LaTeX math via KaTeX (`$$...$$` blocks + `$...$` inline),
 *   - DOMPurify sanitisation (math HTML is trusted and re-inserted after
 *     sanitisation so KaTeX markup survives).
 *
 * Inputs:
 *   - `src`         absolute or app-relative URL of the .md file
 *   - `scrollTo`    optional anchor slug (without the leading `#`). The
 *                   viewer scrolls to it after render, and re-scrolls if
 *                   the input changes. Emits nothing.
 */
@Component({
  selector: 'app-markdown-viewer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [CommonModule],
  template: `
    @if (loading()) {
      <div class="md-loading">Loading…</div>
    }
    @if (error()) {
      <div class="md-error">Failed to load methodology: {{ error() }}</div>
    }
    <div #content class="md-content" [innerHTML]="rendered()"></div>
  `,
  styles: [`
    :host {
      display: block;
      color: var(--text-primary);
    }

    .md-loading, .md-error {
      padding: 16px;
      color: var(--text-muted);
      font-size: 0.88rem;
    }
    .md-error { color: var(--bear); }

    .md-content {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
      font-size: 0.9rem;
      line-height: 1.65;
      color: var(--text-secondary);

      ::ng-deep {
        h1 {
          font-size: 1.75rem;
          color: var(--text-primary);
          letter-spacing: -0.02em;
          font-weight: 700;
          margin: 0 0 14px;
          padding-bottom: 10px;
          border-bottom: 1px solid var(--border);
        }
        h2 {
          font-size: 1.35rem;
          color: var(--text-primary);
          font-weight: 600;
          margin: 28px 0 12px;
          padding-bottom: 6px;
          border-bottom: 1px solid var(--border);
          letter-spacing: -0.01em;
        }
        h3 {
          font-size: 1.12rem;
          color: var(--text-primary);
          font-weight: 600;
          margin: 22px 0 10px;
        }
        h4 {
          font-size: 0.98rem;
          color: var(--text-primary);
          font-weight: 600;
          margin: 18px 0 8px;
        }
        h5 {
          font-size: 0.88rem;
          color: var(--text-secondary);
          font-weight: 600;
          margin: 14px 0 6px;
          text-transform: uppercase;
          letter-spacing: 0.04em;
        }

        h1, h2, h3, h4, h5, h6 {
          scroll-margin-top: 20px;
        }

        h1.highlight, h2.highlight, h3.highlight, h4.highlight {
          background: rgba(59, 130, 246, 0.08);
          padding-left: 8px;
          border-left: 3px solid var(--accent);
          transition: background 1.2s ease-out;
        }

        p { margin: 0 0 12px; }
        p + p { margin-top: 0; }

        ul, ol {
          margin: 0 0 12px;
          padding-left: 20px;

          li { margin-bottom: 4px; }
        }

        /* Plain inline code — monospace, subtle background. */
        code {
          font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
          font-size: 0.82em;
          background: var(--bg-elevated);
          padding: 1px 5px;
          border-radius: 3px;
          color: var(--text-primary);
        }

        /* Fenced code blocks — no background chip inheritance. */
        pre {
          background: var(--bg-elevated);
          border: 1px solid var(--border);
          border-radius: 6px;
          padding: 10px 14px;
          overflow-x: auto;
          margin: 0 0 14px;
          font-size: 0.82rem;
          line-height: 1.55;

          code {
            background: transparent;
            padding: 0;
            border-radius: 0;
            font-size: inherit;
          }
        }

        /* Blockquotes (used for doc intros / callouts). */
        blockquote {
          margin: 0 0 14px;
          padding: 10px 14px;
          border-left: 3px solid var(--accent);
          background: rgba(59, 130, 246, 0.06);
          color: var(--text-secondary);
          border-radius: 0 6px 6px 0;
          font-style: normal;

          p:last-child { margin-bottom: 0; }
        }

        a {
          color: var(--accent);
          text-decoration: none;

          &:hover { text-decoration: underline; }
        }

        /* Tables */
        table {
          width: 100%;
          border-collapse: collapse;
          margin: 0 0 16px;
          font-size: 0.84rem;

          th, td {
            padding: 7px 10px;
            border: 1px solid var(--border);
            text-align: left;
            vertical-align: top;
          }
          th {
            background: var(--bg-elevated);
            color: var(--text-primary);
            font-weight: 600;
          }
          td { color: var(--text-secondary); }
          tr:hover td { background: var(--bg-hover); }
        }

        hr {
          border: 0;
          border-top: 1px solid var(--border);
          margin: 20px 0;
        }

        /* KaTeX overrides for dark theme legibility. */
        .katex-display {
          margin: 10px 0 14px;
          overflow-x: auto;
          overflow-y: hidden;
          padding: 8px 0;
        }
        .katex {
          color: var(--text-primary);
          font-size: 1em;
        }

        strong, b { color: var(--text-primary); font-weight: 600; }
      }
    }
  `],
})
export class MarkdownViewerComponent {
  src = input.required<string>();
  scrollTo = input<string | null>(null);

  private http = inject(HttpClient);
  private destroyRef = inject(DestroyRef);
  private contentEl = viewChild<ElementRef<HTMLDivElement>>('content');

  rendered = signal<string>('');
  loading = signal<boolean>(false);
  error = signal<string | null>(null);

  private marked = this.createMarkedInstance();

  constructor() {
    // Load + render whenever the src changes.
    effect(() => {
      const url = this.src();
      if (!url) return;
      this.fetchAndRender(url);
    });

    // Scroll to anchor when the rendered content is ready OR the anchor changes.
    effect(() => {
      // Depend on both so the effect re-runs on either signal's change.
      this.rendered();
      const anchor = this.scrollTo();
      if (!anchor) return;
      // Wait one animation frame so the DOM is painted.
      queueMicrotask(() => this.scrollToAnchor(anchor));
    });
  }

  private fetchAndRender(url: string): void {
    this.loading.set(true);
    this.error.set(null);

    this.http
      .get(url, { responseType: 'text' })
      .pipe(
        takeUntilDestroyed(this.destroyRef),
        catchError(err => {
          this.error.set(err?.message ?? 'unknown error');
          return of('');
        }),
      )
      .subscribe(md => {
        this.loading.set(false);
        if (!md) return;
        try {
          const html = this.renderMarkdownWithMath(md);
          this.rendered.set(html);
        } catch (e: unknown) {
          this.error.set(e instanceof Error ? e.message : 'render failed');
        }
      });
  }

  /**
   * Render markdown with math. LaTeX blocks are extracted first (so marked
   * doesn't mangle them), rendered by KaTeX, then re-inserted post-sanitise.
   */
  private renderMarkdownWithMath(src: string): string {
    const blocks: string[] = [];
    const inlines: string[] = [];

    // Extract $$...$$ math blocks (non-greedy, multiline). Placeholder must
    // survive marked without being interpreted as markdown — an HTML comment
    // is safe.
    let prepared = src.replace(/\$\$([\s\S]+?)\$\$/g, (_m, tex: string) => {
      const i = blocks.length;
      blocks.push(tex);
      return `\n\n<!--MATHBLOCK:${i}-->\n\n`;
    });
    // Extract inline $...$ math.
    prepared = prepared.replace(/(^|[^\\$])\$([^\n$]+?)\$/g, (_m, lead: string, tex: string) => {
      const i = inlines.length;
      inlines.push(tex);
      return `${lead}<!--MATHINLINE:${i}-->`;
    });

    let html = this.marked.parse(prepared, { async: false }) as string;

    // Substitute math placeholders with rendered KaTeX HTML.
    html = html.replace(/<!--MATHBLOCK:(\d+)-->/g, (_m, idx: string) => {
      const tex = blocks[Number(idx)];
      return this.renderTex(tex, true);
    });
    html = html.replace(/<!--MATHINLINE:(\d+)-->/g, (_m, idx: string) => {
      const tex = inlines[Number(idx)];
      return this.renderTex(tex, false);
    });

    // Add GitHub-style ID anchors to every heading. Doing this post-render
    // is simpler than customising marked's renderer (the v18 renderer API
    // requires access to the parser internals).
    html = this.addHeadingAnchors(html);

    // Sanitise the assembled HTML but allow KaTeX + marked output. We allow
    // all standard tags; DOMPurify by default allows a safe set.
    return DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true, mathMl: true, svg: true },
      ADD_ATTR: ['id', 'class', 'style', 'aria-hidden', 'role'],
    });
  }

  /** Post-render hook that injects a slug `id` attribute on every heading. */
  private addHeadingAnchors(html: string): string {
    return html.replace(
      /<(h[1-6])>([\s\S]*?)<\/\1>/gi,
      (_m, tag: string, inner: string) => {
        // Strip inline tags to get plain text for slug generation.
        const plain = inner.replace(/<[^>]+>/g, '');
        const id = this.slugify(plain);
        return `<${tag} id="${id}">${inner}</${tag}>`;
      },
    );
  }

  private renderTex(tex: string, display: boolean): string {
    try {
      return katex.renderToString(tex, {
        displayMode: display,
        throwOnError: false,
        output: 'html',
      });
    } catch {
      return `<code>${this.escape(tex)}</code>`;
    }
  }

  private createMarkedInstance(): Marked {
    return new Marked({
      gfm: true,
      breaks: false,
    });
  }

  /**
   * Matches the anchor format already used in the doc's table of contents.
   * E.g. "3.1 Daily Information Coefficient" → "31-daily-information-coefficient".
   */
  private slugify(text: string): string {
    return text
      .toLowerCase()
      .replace(/[^\w\s-]/g, '')
      .trim()
      .replace(/\s+/g, '-')
      .replace(/^-+|-+$/g, '');
  }

  private escape(s: string): string {
    return s
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;');
  }

  private scrollToAnchor(anchor: string): void {
    const root = this.contentEl()?.nativeElement;
    if (!root) return;
    const target = root.querySelector('#' + CSS.escape(anchor)) as HTMLElement | null;
    if (!target) return;

    target.scrollIntoView({ behavior: 'smooth', block: 'start' });

    // Briefly highlight the landed-on heading so the reader's eye finds it.
    target.classList.add('highlight');
    setTimeout(() => target.classList.remove('highlight'), 1400);
  }
}
