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
import { HttpClient } from '@angular/common/http';
import { DomSanitizer, type SafeHtml } from '@angular/platform-browser';
import { takeUntilDestroyed } from '@angular/core/rxjs-interop';
import { catchError, of } from 'rxjs';
import { Marked } from 'marked';
import DOMPurify from 'dompurify';
import katex from 'katex';

import { markdownSlug } from '../markdown/markdown-slug';

// The rendered HTML is marked trusted (see `renderMarkdownWithMath`), so the
// viewer renders only the repo-authored documents the app itself serves.
const SERVED_DOCS_ROOT = '/assets/docs/';
const XLINK_NAMESPACE = 'http://www.w3.org/1999/xlink';

/**
 * Renders a markdown file from a URL with:
 *   - GitHub-style heading anchors (`1.` → `#1-...`),
 *   - LaTeX math via KaTeX (`$$...$$` blocks + `$...$` inline),
 *   - DOMPurify sanitisation (math HTML is trusted and re-inserted after
 *     sanitisation so KaTeX markup survives),
 *   - same-document links (`#section`) that scroll within the rendered
 *     document.
 *
 * Inputs:
 *   - `src`         path of a .md file served under `/assets/docs/`; any
 *                   other source is refused
 *   - `scrollTo`    optional anchor slug (without the leading `#`). The
 *                   viewer scrolls to it after render, and re-scrolls if
 *                   the input changes. Emits nothing.
 */
@Component({
  selector: 'app-markdown-viewer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  template: `
    @if (loading()) {
      <div class="md-loading">Loading…</div>
    }
    @if (error()) {
      <div class="md-error">Failed to load methodology: {{ error() }}</div>
    }
    <div #content class="md-content" [innerHTML]="rendered()"></div>
  `,
  styleUrl: './markdown-viewer.component.scss',
  host: { '(click)': 'followInPageLink($event)' },
})
export class MarkdownViewerComponent {
  src = input.required<string>();
  scrollTo = input<string | null>(null);

  private http = inject(HttpClient);
  private destroyRef = inject(DestroyRef);
  private sanitizer = inject(DomSanitizer);
  private contentEl = viewChild<ElementRef<HTMLDivElement>>('content');

  rendered = signal<SafeHtml>('');
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
    if (!url.startsWith(SERVED_DOCS_ROOT) || url.includes('..')) {
      this.error.set(`refusing ${url}: only documents under ${SERVED_DOCS_ROOT} are rendered`);
      return;
    }
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
  private renderMarkdownWithMath(src: string): SafeHtml {
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
    const clean = DOMPurify.sanitize(html, {
      USE_PROFILES: { html: true, mathMl: true, svg: true },
      ADD_ATTR: ['id', 'class', 'style', 'aria-hidden', 'role'],
      // A document never needs page-wide CSS or form controls.
      FORBID_TAGS: ['style', 'form', 'input', 'button', 'select', 'textarea'],
    });
    // DOMPurify is this component's sanitiser of record. Angular's own
    // `[innerHTML]` pass has a narrower allowlist and would silently strip
    // the SVG diagrams and inline styles (KaTeX layout) kept above.
    return this.sanitizer.bypassSecurityTrustHtml(clean);
  }

  /**
   * A same-document link (`#section`) resolves against `<base href="/">`, so
   * the browser would leave the page for `/#section`. Scroll within the
   * rendered document instead. The URL is left alone because the viewer also
   * runs inside a drawer, where the host page's URL is not the document's.
   */
  protected followInPageLink(event: MouseEvent): void {
    const link = event.target instanceof Element ? event.target.closest('a') : null;
    // SVG exported from drawing tools may still use the older `xlink:href`.
    const href = link?.getAttribute('href') ?? link?.getAttributeNS(XLINK_NAMESPACE, 'href');
    if (!href?.startsWith('#') || href.length === 1) return;
    event.preventDefault();
    // Heading ids are slugs (`markdownSlug`), so the fragment needs no decoding.
    this.scrollToAnchor(href.slice(1));
  }

  /** Post-render hook that injects a slug `id` attribute on every heading.
   *
   * Honors the Pandoc/GitBook `{#explicit-id}` anchor syntax in headings:
   * if the heading text ends with `{#some-id}`, that id is used verbatim and
   * the `{#...}` suffix is stripped from the visible text.  Otherwise the id
   * is derived from the heading text via `markdownSlug`.
   */
  private addHeadingAnchors(html: string): string {
    // Matches an explicit anchor suffix — `{#id-slug}` — at the end of
    // the heading text (after stripping tags).  The `{`, `#`, and `}` are
    // rendered by marked as literal characters, so we match them in the
    // post-render HTML.
    const EXPLICIT_ANCHOR = /\{#([\w-]+)\}\s*$/;

    return html.replace(
      /<(h[1-6])>([\s\S]*?)<\/\1>/gi,
      (_m, tag: string, inner: string) => {
        // Strip inline tags to get plain text for id/display resolution.
        const plain = inner.replace(/<[^>]+>/g, '');
        const match = EXPLICIT_ANCHOR.exec(plain);
        if (match) {
          // Use the explicit id; strip the `{#id}` suffix from the displayed text.
          const id = match[1];
          const cleanInner = inner.replace(/\s*\{#[\w-]+\}\s*$/, '');
          return `<${tag} id="${id}">${cleanInner}</${tag}>`;
        }
        const id = markdownSlug(plain);
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
