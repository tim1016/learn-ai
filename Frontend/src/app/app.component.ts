import { ChangeDetectionStrategy, Component, computed, effect, inject } from '@angular/core';
import { Title } from '@angular/platform-browser';
import { ActivatedRouteSnapshot, Router, RouterOutlet } from '@angular/router';
import { Toast } from 'primeng/toast';
import { AlpacaLiveBannerComponent } from './shell/alpaca-live-banner.component';
import { LaneAttentionBellComponent } from './shell/lane-attention-bell.component';
import { MarkdownDrawerHostComponent } from './shared/markdown-drawer/markdown-drawer-host.component';
import { AlpacaLiveVerdictService } from './services/alpaca-live-verdict.service';
import { LaneAttentionService } from './services/lane-attention.service';
import { FleetDirectoryService } from './fleet/fleet-directory.service';
import { AppMenubarComponent } from './shell/app-menubar.component';
import { TopBarComponent } from './shell/top-bar.component';
import { PageBodyComponent } from './shell/page-body.component';
import { LensTabsComponent } from './shared/lens/lens-tabs.component';
import { ActiveLensBridgeService } from './shared/lens/active-lens-bridge.service';
import { pageTitleFor } from './shell/app-menu';
import { CurrentUrlService } from './shell/current-url.service';
import { WorkspaceTitleContextService } from './shell/workspace-title-context.service';
import {
  accountWorkspaceLocation,
  accountWorkspaceTitle,
} from './fleet/account-workspace';
import { laneDisplayNameText } from './fleet/fleet-directory.types';

// The global JobsDrawer / floating "Jobs" launcher was removed in favor
// of per-feature SSE-driven progress UIs (e.g. the Engine Lab run
// banner). JobsService stays mounted via providedIn:'root' so features
// can still consume Jobs SSE without a shared drawer surface.
@Component({
  selector: 'app-root',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [
    RouterOutlet,
    AppMenubarComponent,
    AlpacaLiveBannerComponent,
    LaneAttentionBellComponent,
    TopBarComponent,
    PageBodyComponent,
    MarkdownDrawerHostComponent,
    LensTabsComponent,
    Toast,
  ],
  styles: [`
    :host {
      display: flex;
      min-height: 100dvh;
      background: var(--bg-canvas);
      color: var(--text-primary);
    }

    .shell {
      display: flex;
      flex: 1;
      min-width: 0;
      min-height: 100dvh;
      flex-direction: column;
    }

    .shell-actions {
      display: flex;
      min-width: 0;
      max-width: 100%;
      align-items: center;
      justify-content: flex-end;
      gap: var(--space-2);
      overflow-x: auto;
      white-space: nowrap;
      /* The live-verdict pills are the account-mode trust anchor (ADR 0059
         D8): one badge per lane (FleetDirectoryService.lanesOf('alpaca')),
         so the row's natural width grows with the fleet. The header's
         minmax(0, 1fr) side columns let content overflow past their own
         track rather than being clamped, which used to bleed the pills
         under the centered Botasur logo at in-between widths. Wrapping kept
         every pill inside this column, but let the header itself grow past
         its fixed height whenever the row needed a second line — so this
         column scrolls horizontally within its own track instead: it never
         bleeds past the track and the header height stays fixed. */
      flex-wrap: nowrap;
    }

    @media (max-width: 760px) {
      .shell-actions {
        gap: 3px;
      }
    }

    /* Shrinks the shared pill toggle (its own default look, unmodified) to
       sit comfortably beside the live-mode pills in the top bar. */
    .shell-lens-toggle ::ng-deep .lens-tabs {
      padding: 2px;
    }

    .shell-lens-toggle ::ng-deep .lens-tabs__tab {
      min-height: auto;
      padding: 0.3rem 0.75rem;
      font-size: var(--fs-xs);
    }

    /* Named container "ide" drives the .ide-grid breakpoints declared in
       styles.scss. Lives here (outside any per-page component) so
       container-query measurement is unaffected by tab switches, modal
       mounts, or page-level transforms. */
    .main {
      flex: 1;
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
      overflow-x: auto;
      container: ide / inline-size;
    }

    .main-content {
      flex: 1;
      min-width: 0;
      min-height: 0;
      display: flex;
      flex-direction: column;
    }
  `],
  template: `
    <div class="shell">
      <app-top-bar>
        <app-menubar shell-nav />
        <nav class="shell-actions" shell-connection aria-label="Quick links and account status">
          @if (lensHost(); as host) {
            <app-lens-tabs
              class="shell-lens-toggle"
              [ariaLabel]="host.ariaLabel ?? 'Desk perspective'"
              [idPrefix]="host.idPrefix ?? ''"
              [panelId]="host.panelId ?? null"
              [lens]="host.lens()"
              (lensChange)="host.select($event)"
            />
          }
          @for (lane of alpacaLanes(); track lane.clerk_id) {
            <app-alpaca-live-banner [lane]="lane" />
            <app-lane-attention-bell [lane]="lane" />
          } @empty {
            <!-- Never zero badges. An unresolved roster is itself an
                 undetermined mode, and the banner says so loudly. -->
            <app-alpaca-live-banner [lane]="null" />
          }
        </nav>
      </app-top-bar>
      <main class="main">
        <div class="main-content">
          <app-page-body [fullBleed]="isFullBleedRoute()">
            <router-outlet />
          </app-page-body>
        </div>
      </main>
    </div>
    <app-markdown-drawer-host />
    <p-toast position="top-right" />
  `,
})
export class AppComponent {
  private readonly alpacaLive = inject(AlpacaLiveVerdictService);
  private readonly laneAttention = inject(LaneAttentionService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly title = inject(Title);
  private readonly router = inject(Router);
  private readonly currentUrl = inject(CurrentUrlService).url;
  private readonly titleContext = inject(WorkspaceTitleContextService);
  private readonly lensBridge = inject(ActiveLensBridgeService);

  /** The one Trader/Operator toggle, shown only while some page has
   * registered as its host (ActiveLensBridgeService) — never a dead control
   * on pages with no lens. */
  protected readonly lensHost = this.lensBridge.host;

  /** Inside an account workspace the window names what is open and the account
   * it is open on — "Gallery · Paper" (ADR 0064 Decision 6). The account's
   * *name* is `laneDisplayName`, never its Paper/Live mode: the two are
   * separate facts that only read alike while a lane has no nickname.
   *
   * Everything a workspace title needs is in the URL except the open bot's
   * label, which only the bot panel holds; it writes that into
   * `WorkspaceTitleContextService` while its page is open. Reading it only
   * when the URL names a bot is what keeps a stale label from ever reaching a
   * tab's title.
   *
   * Deploy is one of the tabs (ADR 0064 Decision 1 extended), so opening it
   * renames the window exactly as switching to Bots or Gallery does. */
  private readonly workspaceTitle = computed(() => {
    const location = accountWorkspaceLocation(this.currentUrl());
    if (location === null) return null;
    const name = this.fleetDirectory.displayNameOf(location.broker, location.clerkId);
    return accountWorkspaceTitle(
      location.tab,
      name === null ? null : laneDisplayNameText(name),
      location.botSid === null ? null : this.titleContext.botLabel(),
    );
  });

  protected readonly pageTitle = computed(
    () => this.workspaceTitle() ?? pageTitleFor(this.currentUrl()),
  );
  protected readonly alpacaLanes = computed(() => this.fleetDirectory.lanesOf('alpaca'));
  protected readonly isFullBleedRoute = computed(() => {
    this.currentUrl();
    return activeRouteHasData(this.router.routerState.snapshot.root, 'fullBleed');
  });

  constructor() {
    effect(() => this.title.setTitle(this.pageTitle() ?? 'Botasur'));
    // The Alpaca account-mode banner is the ADR 0011 trust anchor for the
    // Alpaca path (ADR 0059 D8): one root poll, rendered from the server
    // verdict, never composed on the client.
    this.alpacaLive.start();
    // One aggregate poll behind every per-lane attention bell (#2228): the
    // coordinator fans the read out to each lane server-side, and each bell
    // renders only its own lane's slice of the fold.
    this.laneAttention.start();
  }
}

function activeRouteHasData(route: ActivatedRouteSnapshot, key: string): boolean {
  return Boolean(route.data[key]) || route.children.some((child) => activeRouteHasData(child, key));
}
