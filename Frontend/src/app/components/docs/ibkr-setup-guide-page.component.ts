import { ChangeDetectionStrategy, Component, inject, signal } from '@angular/core';
import { ActivatedRoute } from '@angular/router';
import { toSignal } from '@angular/core/rxjs-interop';
import { map } from 'rxjs/operators';
import { MarkdownViewerComponent } from '../../shared/markdown-viewer/markdown-viewer.component';
import { PageHeaderComponent } from '../../shared/page-header/page-header.component';

/**
 * `/docs/ibkr-setup-guide` renders the operator-facing copy of the
 * canonical repo guide at `docs/runbooks/ibkr-setup-guide.md`.
 */
@Component({
  selector: 'app-ibkr-setup-guide-page',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [MarkdownViewerComponent, PageHeaderComponent],
  template: `
    <div class="guide-page">
      <app-page-header
        eyebrow="Runbook"
        title="IBKR Setup Guide"
        subtitle="Paper Gateway / TWS setup, API socket settings, client-ID separation, diagnostics, and account monitor cutover checks."
      >
        <a
          slot="actions"
          class="page-link"
          href="/assets/docs/ibkr-setup-guide.md"
          target="_blank"
          rel="noopener"
        >
          Raw markdown <i class="pi pi-external-link" aria-hidden="true"></i>
        </a>
      </app-page-header>

      <app-markdown-viewer [src]="src()" [scrollTo]="fragment()" />
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
export class IbkrSetupGuidePageComponent {
  private readonly route = inject(ActivatedRoute);

  readonly src = signal('/assets/docs/ibkr-setup-guide.md');
  readonly fragment = toSignal(this.route.fragment.pipe(map((f) => f ?? null)), {
    initialValue: null,
  });
}
