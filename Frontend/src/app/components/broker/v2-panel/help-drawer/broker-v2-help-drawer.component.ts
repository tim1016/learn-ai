import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { Drawer } from 'primeng/drawer';
import { ButtonModule } from 'primeng/button';
import { RouterLink } from '@angular/router';
import { MarkdownViewerComponent } from '../../../../shared/markdown-viewer/markdown-viewer.component';
import { BrokerV2HelpDrawerService } from './broker-v2-help-drawer.service';

/**
 * Right-side slide-in drawer showing the broker-v2 operator manual at a
 * specific section. Mounted once at the app-shell level. Use
 * `BrokerV2HelpDrawerService.open(anchor)` from anywhere to show the manual
 * at a specific section.
 *
 * Canonical pattern: `shared/methodology-drawer/methodology-drawer.component.ts`.
 * Duplication is intentional: different markdown source, title, and full-page
 * link. Both are shell-level singletons serving distinct documents. See
 * `BrokerV2HelpDrawerService` provenance comment for the generalization note.
 */
@Component({
  selector: 'app-broker-v2-help-drawer',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Drawer, ButtonModule, RouterLink, MarkdownViewerComponent],
  template: `
    <p-drawer
      [visible]="svc.visible()"
      (visibleChange)="onVisibleChange($event)"
      position="right"
      class="broker-v2-help-drawer"
      [showCloseIcon]="false"
      [modal]="true"
      [dismissible]="true"
      [style]="{ width: 'min(800px, 92vw)' }"
    >
      <ng-template #header>
        <div class="drawer-header">
          <span class="drawer-eyebrow mono">Operator Manual</span>
          <h3 class="drawer-title">Broker V2 Panel</h3>
          <div class="drawer-actions">
            <a
              class="drawer-link"
              routerLink="/brokers/alpaca/manual"
              target="_blank"
              rel="noopener"
            >
              Open full manual <i class="pi pi-external-link" aria-hidden="true"></i>
            </a>
            <button
              type="button"
              class="drawer-close"
              (click)="svc.close()"
              aria-label="Close operator manual drawer"
            >
              <i class="pi pi-times" aria-hidden="true"></i>
            </button>
          </div>
        </div>
      </ng-template>

      <app-markdown-viewer
        [src]="'/assets/docs/broker-v2-operator-manual.md'"
        [scrollTo]="svc.anchor()"
      />
    </p-drawer>
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

      :host ::ng-deep .broker-v2-help-drawer {
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
export class BrokerV2HelpDrawerComponent {
  protected svc = inject(BrokerV2HelpDrawerService);

  onVisibleChange(v: boolean): void {
    if (!v) this.svc.close();
  }
}
