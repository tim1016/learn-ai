import { ChangeDetectionStrategy, Component, inject } from '@angular/core';
import { Drawer } from 'primeng/drawer';
import { ButtonModule } from 'primeng/button';
import { MarkdownViewerComponent } from '../markdown-viewer/markdown-viewer.component';
import { MethodologyDrawerService } from './methodology-drawer.service';

/**
 * Right-side slide-in drawer hosting the methodology markdown viewer.
 * Mounted once at the app-shell level. Use `MethodologyDrawerService.open(anchor)`
 * from anywhere to show the doc at a specific section.
 */
@Component({
  selector: 'app-methodology-drawer',
  standalone: true,
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [Drawer, ButtonModule, MarkdownViewerComponent],
  template: `
    <p-drawer
      [visible]="svc.visible()"
      (visibleChange)="onVisibleChange($event)"
      position="right"
      [styleClass]="'methodology-drawer'"
      [showCloseIcon]="false"
      [modal]="true"
      [dismissible]="true"
      [style]="{ width: 'min(960px, 92vw)' }"
    >
      <ng-template pTemplate="header">
        <div class="drawer-header">
          <span class="drawer-eyebrow mono">Reference</span>
          <h3 class="drawer-title">Indicator Reliability — Methodology</h3>
          <div class="drawer-actions">
            <a class="drawer-link" href="/docs/indicator-reliability-methodology" target="_blank" rel="noopener">
              Open full page <i class="pi pi-external-link" aria-hidden="true"></i>
            </a>
            <button
              type="button"
              class="drawer-close"
              (click)="svc.close()"
              aria-label="Close methodology drawer"
            >
              <i class="pi pi-times" aria-hidden="true"></i>
            </button>
          </div>
        </div>
      </ng-template>

      <app-markdown-viewer
        [src]="'/assets/docs/indicator-reliability-methodology.md'"
        [scrollTo]="svc.anchor()"
      />
    </p-drawer>
  `,
  styles: [`
    .drawer-header {
      display: grid;
      grid-template-columns: 1fr auto;
      grid-template-areas:
        "eyebrow actions"
        "title   actions";
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

      &:hover { background: var(--bg-hover); }
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

      &:hover { background: var(--bg-hover); color: var(--text-primary); }
    }

    .mono {
      font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace;
      font-variant-numeric: tabular-nums;
    }

    :host ::ng-deep .methodology-drawer {
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
  `],
})
export class MethodologyDrawerComponent {
  protected svc = inject(MethodologyDrawerService);

  onVisibleChange(v: boolean): void {
    if (!v) this.svc.close();
  }
}
