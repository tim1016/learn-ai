import {
  ChangeDetectionStrategy,
  Component,
  ElementRef,
  computed,
  inject,
  input,
  signal,
} from '@angular/core';
import { RouterLink } from '@angular/router';

import {
  accountWorkspaceBotRoute,
  type AccountWorkspaceLink,
} from '../fleet/account-workspace';
import {
  laneConfirmedAccount,
  type LaneDescriptor,
} from '../fleet/fleet-directory.types';
import { AssetIdentityComponent } from '../shared/asset-identity/asset-identity.component';
import { ReceiptLabelPipe } from '../shared/pipes/receipt-label.pipe';
import {
  LaneAttentionService,
  type LaneAttentionItem,
} from '../services/lane-attention.service';

/** The three treatments a bell renders in. The severity word is always in
 * the item text too — colour is never the only signal (WCAG 1.4.1). */
type BellTone = 'is-blocking' | 'is-warning' | 'is-unknown';

/** One popover row: the server's item plus this client's way into the bot
 * it names (`null` when the item names no bot — an account-scoped condition
 * has no bot page to open). */
interface BellItem {
  readonly item: LaneAttentionItem;
  /** Severity as a word, shown beside the colour: 'Blocking' | 'Warning'. */
  readonly severityWord: string;
  readonly link: AccountWorkspaceLink | null;
}

/**
 * One lane's attention bell (#2228), mounted beside that lane's
 * `app-alpaca-live-banner` chip in the shell — never merged across lanes.
 *
 * Renders nothing while the lane is quiet: the bell is a condition surface,
 * not a standing mode fact like the badge beside it, so absence of a bell is
 * the healthy state and cannot be confused with the explicit grey *unknown*
 * treatment the lane earns when its own read failed (`ok: false` in the
 * aggregate — the coordinator's judgment, never guessed here from transport
 * faults).
 *
 * Items are the lane's clerk's own headlines (operator prose from the
 * server, so never through `receiptLabel`); `reason_code` is a backend
 * identifier and goes through the shared pipe; a symbol renders through
 * `app-asset-identity` at its compact size. Each item links into its bot's
 * workspace page when the item names a strategy and the lane's account is
 * confirmed, and an item disappears exactly when its condition resolves —
 * the bell's count is `condition_id`-deduped, so it clears only when fixed.
 */
@Component({
  selector: 'app-lane-attention-bell',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [RouterLink, AssetIdentityComponent, ReceiptLabelPipe],
  host: {
    '(document:mousedown)': 'onDocumentMousedown($event)',
    '(keydown.escape)': 'onEscape()',
  },
  styles: [`
    :host { display: inline-flex; }
    .bell {
      position: relative;
      display: inline-flex; align-items: center; justify-content: center;
      min-height: 30px; padding: 0 0.45rem;
      border-radius: var(--radius-pill); border: 1px solid; cursor: pointer;
      background: rgba(5, 8, 14, 0.42); color: var(--text-primary);
      font-size: var(--fs-xs); line-height: 1.2; white-space: nowrap;
      font-variant-numeric: tabular-nums; font-weight: 700;
      transition: box-shadow 0.12s ease, border-color 0.12s ease;
    }
    .bell:focus-visible { outline: 2px solid var(--p-primary-color, #6da2ff); outline-offset: 2px; }
    .bell.is-blocking { color: #fff; border-color: #ff8b88; background: rgba(90, 12, 20, 0.72); }
    .bell.is-warning { color: #fff; border-color: #ffb020; background: rgba(122, 61, 0, 0.85); }
    .bell.is-unknown { color: #d7d9de; border-color: rgba(178, 181, 190, 0.45); font-weight: 500; }
    .bell__count { margin-left: 0.25rem; }
    .panel {
      position: absolute; top: calc(100% + 6px); right: 0; z-index: 60;
      min-width: 280px; max-width: 380px; padding: 0.55rem 0.65rem;
      border-radius: 8px; border: 1px solid rgba(178, 181, 190, 0.45);
      background: var(--panel-bg, #10141c); color: var(--text-primary);
      box-shadow: 0 8px 24px rgba(0, 0, 0, 0.5);
      font-weight: 400; text-align: left;
    }
    .panel__heading { font-size: var(--fs-xs); font-weight: 700; margin-bottom: 0.4rem; }
    .panel__list { display: flex; flex-direction: column; gap: 0.45rem; list-style: none; margin: 0; padding: 0; }
    .item { display: flex; flex-direction: column; gap: 0.15rem; }
    .item + .item { border-top: 1px solid rgba(178, 181, 190, 0.2); padding-top: 0.45rem; }
    .item__meta { display: flex; align-items: center; gap: 0.4rem; font-size: var(--fs-xs); }
    .item__severity { font-weight: 700; }
    .item__severity.is-blocking { color: #ff8b88; }
    .item__severity.is-warning { color: #ffb020; }
    .item__reason { opacity: 0.75; }
    .item__headline { font-size: var(--fs-xs); line-height: 1.35; }
    .item__open { font-size: var(--fs-xs); color: var(--p-primary-color, #6da2ff); text-decoration: none; width: fit-content; }
    .item__open:hover { text-decoration: underline; }
    .item__open:focus-visible { outline: 2px solid var(--p-primary-color, #6da2ff); outline-offset: 2px; }
    .panel__unknown { font-size: var(--fs-xs); line-height: 1.35; }
  `],
  template: `
    @if (view(); as v) {
      <button
        type="button"
        class="bell"
        [class]="v.tone"
        aria-haspopup="dialog"
        [attr.aria-expanded]="open()"
        [attr.aria-label]="v.ariaLabel"
        (click)="toggle()"
      >
        <span aria-hidden="true">{{ v.glyph }}</span>
        @if (v.count !== null) {
          <span class="bell__count">{{ v.count }}</span>
        }
      </button>
      @if (open()) {
        <div class="panel" role="dialog" [attr.aria-label]="v.panelLabel">
          @if (v.unknown) {
            <p class="panel__unknown">
              This lane's attention read could not be completed, so its
              conditions — if any — are unknown. This is never the same as
              quiet. @if (v.errorReason; as reason) {
                Read failed: <span>{{ reason | receiptLabel }}</span>.
              }
            </p>
          } @else {
            <p class="panel__heading">{{ v.heading }}</p>
            <ul class="panel__list">
              @for (row of v.items; track row.item.condition_id) {
                <li class="item">
                  <span class="item__meta">
                    <span class="item__severity" [class]="row.severityClass">{{ row.severityWord }}</span>
                    @if (row.item.symbol; as symbol) {
                      <app-asset-identity [symbol]="symbol" size="xs" />
                    }
                    <span class="item__reason">{{ row.item.reason_code | receiptLabel }}</span>
                  </span>
                  <span class="item__headline">{{ row.item.headline }}</span>
                  @if (row.link; as link) {
                    <a class="item__open" [routerLink]="link.commands" [queryParams]="link.queryParams">
                      Open bot
                    </a>
                  }
                </li>
              }
            </ul>
          }
        </div>
      }
    }
  `,
})
export class LaneAttentionBellComponent {
  private readonly attention = inject(LaneAttentionService);

  /** The lane this bell speaks for. The roster-unknown banner case mounts no
   * bell: there is no lane to read conditions for, and an orphan bell would
   * be indistinguishable from a global one — exactly what #2228 removes. */
  readonly lane = input.required<LaneDescriptor>();

  protected readonly open = signal(false);

  protected toggle(): void {
    this.open.update((open) => !open);
  }

  onEscape(): void {
    this.open.set(false);
  }

  onDocumentMousedown(event: MouseEvent): void {
    // The panel and the button that opens it are both inside this host; a
    // press anywhere else closes. mousedown, not click, so the same press
    // can't both close this popover and toggle it back open.
    if (!this.open()) return;
    if (event.target instanceof Node && this.hostContains(event.target)) return;
    this.open.set(false);
  }

  private hostContains(node: Node): boolean {
    let current: Node | null = node;
    while (current !== null) {
      if (current === this.host) return true;
      current = current.parentNode;
    }
    return false;
  }

  private readonly host = inject(ElementRef).nativeElement as HTMLElement;

  /** Everything the template renders, derived once per state change so the
   * tone/word/label mapping lives in exactly one place. */
  protected readonly view = computed<BellView | null>(() => {
    const state = this.attention.stateFor(this.lane().clerk_id);
    if (state.unknown) {
      return {
        tone: 'is-unknown',
        glyph: '?',
        count: null,
        unknown: true,
        errorReason: state.errorReason,
        items: [],
        heading: '',
        panelLabel: 'Attention unknown',
        ariaLabel: 'Lane attention unknown — the last read failed',
      };
    }
    const items = state.items;
    if (items.length === 0) return null;

    const rows = items.map((item) => this.rowFor(item));
    const worst = rows.some((row) => row.severityWord === 'Blocking')
      ? 'is-blocking'
      : 'is-warning';
    const blockingCount = rows.filter((row) => row.severityWord === 'Blocking').length;
    return {
      tone: worst,
      glyph: '!',
      count: items.length,
      unknown: false,
      errorReason: null,
      items: rows,
      heading:
        blockingCount > 0
          ? `${blockingCount} blocking ${pluralize('condition', blockingCount)} on this lane`
          : `${items.length} ${pluralize('condition', items.length)} on this lane`,
      panelLabel: `Lane attention: ${items.length} ${pluralize('condition', items.length)}`,
      ariaLabel:
        blockingCount > 0
          ? `${blockingCount} blocking conditions need attention on this lane`
          : `${items.length} conditions need attention on this lane`,
    };
  });

  /** One popover row: the severity word, its class, and the way into the bot
   * the item names — its bot page when the item carries a strategy id and
   * the lane's account is confirmed, otherwise nothing to link (the banner
   * beside this bell is already the way into the workspace). */
  private rowFor(item: LaneAttentionItem): BellItem & { severityClass: string } {
    const blocking = item.severity === 'blocking';
    const lane = this.lane();
    const accountId = laneConfirmedAccount(lane);
    const link =
      item.strategy_instance_id && accountId
        ? accountWorkspaceBotRoute(
            { broker: lane.broker, clerkId: lane.clerk_id, accountId },
            item.strategy_instance_id,
            'bots',
          )
        : null;
    return {
      item,
      severityWord: blocking ? 'Blocking' : 'Warning',
      severityClass: blocking ? 'is-blocking' : 'is-warning',
      link,
    };
  }
}

interface BellView {
  readonly tone: BellTone;
  readonly glyph: string;
  readonly count: number | null;
  readonly unknown: boolean;
  readonly errorReason: string | null;
  readonly items: readonly (BellItem & { severityClass: string })[];
  readonly heading: string;
  readonly panelLabel: string;
  readonly ariaLabel: string;
}

function pluralize(word: string, count: number): string {
  return count === 1 ? word : `${word}s`;
}
