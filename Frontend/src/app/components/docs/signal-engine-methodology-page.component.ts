import { ChangeDetectionStrategy, Component, signal, inject } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { map } from 'rxjs/operators';
import { MarkdownViewerComponent } from '../../shared/markdown-viewer/markdown-viewer.component';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';

/**
 * Signal Engine — methodology and authority reference. Reached via
 * `/docs/signal-engine-methodology`. URL fragment (`#section-id`) deep-links
 * into a specific section.
 *
 * Sources its content from `Frontend/src/assets/docs/signal-engine-methodology.md`,
 * which is mirrored from the canonical `docs/signal-engine-authority.md`.
 * The repo-root copy is the single source of truth — when changing the math
 * or the graduation thresholds, update the repo-root file and copy it across.
 */
@Component({
  selector: 'app-signal-engine-methodology-page',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MarkdownViewerComponent, PageHeaderComponent],
  template: `
    <div class="methodology-page">
      <app-page-header
        eyebrow="Reference"
        title="Signal Engine — Methodology"
        subtitle="Walk-forward backtesting with strict no-lookahead PnL, Stage 0 kill switch, Lo (2002) Sharpe CIs, Bailey & López de Prado deflated Sharpe, and the 0/1/2/3 graduation ladder."
      >
        <a
          slot="actions"
          class="page-link"
          href="/assets/docs/signal-engine-methodology.md"
          target="_blank"
          rel="noopener"
        >
          Raw markdown <i class="pi pi-external-link" aria-hidden="true"></i>
        </a>
      </app-page-header>

      <app-markdown-viewer
        [src]="src()"
        [scrollTo]="fragment()"
      />
    </div>
  `,
  styles: [
    `
      :host {
        display: block;
        max-width: 960px;
        margin: 0 auto;
        padding: 4px 4px 40px;
      }

      .page-link {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font-size: 0.78rem;
        color: var(--accent);
        text-decoration: none;
        padding: 5px 10px;
        border: 1px solid var(--border);
        border-radius: 4px;
      }

      .page-link:hover {
        background: var(--bg-hover);
      }
    `,
  ],
})
export class SignalEngineMethodologyPageComponent {
  private route = inject(ActivatedRoute);

  readonly src = signal('/assets/docs/signal-engine-methodology.md');

  readonly fragment = toSignal(
    this.route.fragment.pipe(map((f) => f ?? null)),
    { initialValue: null },
  );
}
