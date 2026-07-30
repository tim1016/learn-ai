import { ChangeDetectionStrategy, Component, computed, inject } from '@angular/core';
import { Drawer } from 'primeng/drawer';
import { ButtonModule } from 'primeng/button';
import { RouterLink } from '@angular/router';
import { MarkdownViewerComponent } from '../markdown-viewer/markdown-viewer.component';
import { MarkdownDrawerService } from './markdown-drawer.service';
import { MARKDOWN_DOC_REGISTRY } from './markdown-drawer.model';

/**
 * Single shell-level markdown drawer host.
 *
 * Replaces the former pair `MethodologyDrawerComponent` +
 * `BrokerV2HelpDrawerComponent`.  Mount once at `app-root`; drive with
 * `MarkdownDrawerService.open(docId, anchor?)`.
 *
 * Only one document is ever open at a time — the service enforces this by
 * tracking a single `activeDocId`.
 *
 * Provenance:
 *   - Supersedes `shared/methodology-drawer/methodology-drawer.component.ts`
 *     (S0-era, single-document drawer for the methodology doc).
 *   - Supersedes `components/broker/v2-panel/help-drawer/broker-v2-help-drawer.component.ts`
 *     (S5, duplicate drawer for the operator manual).
 *   - Both old components were shell-level singletons with near-identical
 *     structure (PrimeNG Drawer + MarkdownViewerComponent + close button).
 *     A typed document registry removes the structural duplication while
 *     keeping each document's metadata co-located in the model file.
 */
@Component({
  selector: 'app-markdown-drawer-host',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Drawer, ButtonModule, RouterLink, MarkdownViewerComponent],
  template: `
    @if (doc(); as d) {
      <p-drawer
        [visible]="svc.visible()"
        (visibleChange)="onVisibleChange($event)"
        position="right"
        class="markdown-drawer"
        [showCloseIcon]="false"
        [modal]="true"
        [dismissible]="true"
        [style]="{ width: d.width ?? 'min(960px, 92vw)' }"
      >
        <ng-template #header>
          <div class="drawer-header">
            <span class="drawer-eyebrow mono">{{ d.eyebrow }}</span>
            <h3 class="drawer-title">{{ d.title }}</h3>
            <div class="drawer-actions">
              <a
                class="drawer-link"
                [routerLink]="d.fullPageRoute"
                target="_blank"
                rel="noopener"
              >
                Open full page <i class="pi pi-external-link" aria-hidden="true"></i>
              </a>
              <button
                type="button"
                class="drawer-close"
                (click)="svc.close()"
                aria-label="Close documentation drawer"
              >
                <i class="pi pi-times" aria-hidden="true"></i>
              </button>
            </div>
          </div>
        </ng-template>

        <app-markdown-viewer
          [src]="d.src"
          [scrollTo]="svc.anchor()"
        />
      </p-drawer>
    }
  `,
  styles: [
    `
      .drawer-header {
        display: grid;
        grid-template-columns: 1fr auto;
        grid-template-areas:
          'eyebrow actions'
          'title   actions';
        gap: 4px 12px;
        align-items: center;
        width: 100%;
      }
      .drawer-eyebrow {
        grid-area: eyebrow;
        font-size: 0.65rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.12em;
        color: var(--text-muted);
      }
      .drawer-title {
        grid-area: title;
        margin: 0;
        font-size: 1.1rem;
        font-weight: 600;
        color: var(--text-primary);
        letter-spacing: -0.01em;
      }
      .drawer-actions {
        grid-area: actions;
        display: flex;
        align-items: center;
        gap: 12px;
      }
      .drawer-link {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        font-size: 0.72rem;
        color: var(--accent);
        text-decoration: none;
        padding: 5px 10px;
        border: 1px solid var(--border);
        border-radius: 4px;
        transition: background 0.1s;

        &:hover {
          background: var(--bg-hover);
        }
      }
      .drawer-close {
        background: transparent;
        border: 1px solid var(--border);
        color: var(--text-secondary);
        width: 30px;
        height: 30px;
        border-radius: 4px;
        cursor: pointer;
        display: flex;
        align-items: center;
        justify-content: center;
        transition: background 0.1s;

        &:hover {
          background: var(--bg-hover);
          color: var(--text-primary);
        }
      }

      .mono {
        font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
        font-variant-numeric: tabular-nums;
      }

      :host ::ng-deep .markdown-drawer {
        background: var(--bg-surface);
        border-left: 1px solid var(--border);

        .p-drawer-header {
          padding: 16px 20px;
          border-bottom: 1px solid var(--border);
        }
        .p-drawer-content {
          padding: 20px 24px 40px;
          background: var(--bg-surface);
        }
      }
    `,
  ],
})
export class MarkdownDrawerHostComponent {
  protected readonly svc = inject(MarkdownDrawerService);

  /** Resolves the active document descriptor, or null when no drawer is open. */
  protected readonly doc = computed(() => {
    const id = this.svc.activeDocId();
    return id ? MARKDOWN_DOC_REGISTRY[id] : null;
  });

  onVisibleChange(v: boolean): void {
    if (!v) this.svc.close();
  }
}
