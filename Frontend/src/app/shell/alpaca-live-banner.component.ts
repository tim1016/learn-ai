import { NgTemplateOutlet } from '@angular/common';
import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';
import { RouterLink } from '@angular/router';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import {
  accountWorkspaceBadgeRoute,
  accountWorkspaceLens,
  accountWorkspaceLocation,
  type AccountWorkspaceLink,
} from '../fleet/account-workspace';
import { FleetDirectoryService } from '../fleet/fleet-directory.service';
import {
  laneConfirmedAccount,
  laneDisplayName,
  laneDisplayNameText,
  type LaneDescriptor,
} from '../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService, type LaneVerdictState } from '../services/alpaca-live-verdict.service';
import { formatReceiptLabel } from '../shared/pipes/receipt-label.pipe';
import { CurrentUrlService } from './current-url.service';

/**
 * The standing assumption an undetermined badge carries. It is in the badge's
 * own visible text AND in its accessible name: `aria-label` wins the
 * accessible-name computation and screen readers commonly announce a labelled
 * live region by its name, so a warning whose meaning lives only in the
 * visible copy and the amber is the WCAG 1.4.1 failure this badge exists to
 * prevent. Closed operator copy — a transport/roster fact the server cannot
 * author (ADR 0011 §2; #2110 D2).
 */
const ASSUME_REAL_MONEY = 'Assume real money until a read succeeds.';

/** Which of the four treatments a badge renders in. */
type BadgeTone = 'is-paper' | 'is-live-unarmed' | 'is-live-armed' | 'is-undetermined';

/**
 * Everything known about one lane's badge before its extra facts (shadow
 * authority, armed count, loss hold, refusal code) are folded into the
 * accessible name and tooltip by `finalizeBadge`. Kept separate from
 * `LaneBadge` so those four facts have exactly one place they turn into text
 * — never a second, inline-only rendering that could say something the
 * accessible name doesn't.
 */
interface LaneBadgeFacts {
  readonly tone: BadgeTone;
  readonly laneHint: string | null;
  readonly mode: string;
  readonly ariaLabel: string;
  readonly detail: string;
  readonly shadow: boolean;
  readonly armedCount: number | null;
  readonly lossHold: boolean;
  readonly refusalCode: string | null;
}

/**
 * One rendered badge, built once per change in TypeScript so the closed
 * `final_verdict` union is mapped exhaustively in one place: a fifth verdict
 * added to the contract is a compile error here, rather than warning *text*
 * silently paired with calm *styling* in a template `@else`.
 */
interface LaneBadge {
  readonly tone: BadgeTone;
  /** The compact mode word shown in the pill: "Live", "Paper", or an
   * undetermined-state sentence (which is already the loud, descriptive text
   * WCAG 1.4.1 requires — never a generic "Undetermined" label). */
  readonly mode: string;
  /** A short lane label shown beside `mode`, null unless a sibling of the
   * same broker currently reads the identical mode word (see `badge`'s
   * collision check): with exactly one paper and one live lane, `mode` alone
   * can never collide with a sibling's. */
  readonly laneHint: string | null;
  readonly ariaLabel: string;
  readonly detail: string;
}

/**
 * Compact Alpaca account-mode trust anchor for ONE lane (ADR 0059 D8;
 * ADR 0011; #2110). Renders exactly the lane it is given — the shell mounts
 * one instance per `FleetDirectoryService.lanesOf('alpaca')` entry, never a
 * merged or hardcoded pair, so every badge carries its own lane identity even
 * though the pill itself now shows only the mode word.
 *
 * The server verdict remains the only truth source. Live mode keeps the
 * authority mode and armed count in the accessible name and tooltip even in
 * the dense global header — but not the account number, which names the
 * account without guarding anything here (ADR 0064; #2188).
 *
 * **It is also the way to that account** (ADR 0064 Decision 3): the whole
 * badge is a link into its workspace, on the tab the operator is already
 * standing on. The link wraps the status region rather than replacing it —
 * one element cannot be both a live region and a link, and the warning below
 * is why the live region is the part that must not move.
 *
 * **It never renders nothing.** Four distinct things leave a mode
 * undetermined, and all four render the same LOUD amber warning rather than
 * the banned grey "not configured" styling and rather than silence:
 *
 * 1. the server itself reports `final_verdict: 'unknown'`;
 * 2. this lane's own read failed;
 * 3. no read has completed for this lane yet (~5 s on every boot, and again
 *    whenever a lane joins mid-session);
 * 4. there is no lane at all — the shell passes `null` when the directory is
 *    still loading or its load failed, which on any non-broker route used to
 *    leave the header calm and badge-less for the entire session.
 *
 * An undeterminable lane could be real money, so the badge says so in its own
 * text and in its accessible name, not only through colour (WCAG 1.4.1).
 *
 * Note: the visible pill shows only the compact mode word ("Live" / "Paper" /
 * the undetermined sentence, via `modeWordFor`) — with exactly one paper and
 * one live lane (today's fleet) that alone is unambiguous, and it is what
 * buys back the header's horizontal space. "Paper" is deliberately shorter
 * than `verdictModeChip`'s "Paper money" (alpaca-live-verdict.service.ts):
 * this pill's whole job is compact width in a dense header, the workspace
 * header `AlpacaLaneModeChipComponent` renders can afford the fuller word,
 * and both derive independently from the same `final_verdict` — this
 * accessible name and tooltip still carry the full verdict headline
 * regardless of which short word is on screen, so the two surfaces reading
 * differently costs nothing the operator needs.
 *
 * The lane's own name — `laneDisplayName` (fleet-directory.types): its
 * account nickname, or its `display_label` (set once at clerk enrolment via
 * `provision --label` / `migrate-existing --label`) until one is set — moves
 * into the accessible name and the tooltip instead of disappearing, and
 * reappears in the pill itself the moment a sibling of the same broker
 * currently reads the identical mode word (ADR 0064 Decision 5 extended):
 * with one paper and one live lane the words can never collide, but a second
 * live clerk on the same broker (ADR 0062 Phase 5) makes two pills both read
 * "Live" — checked against siblings' own current mode word via `modeWordFor`,
 * not a raw lane count, so it fires exactly on a genuine collision and never
 * earlier. Both name and disambiguator are operator prose, not backend
 * identifiers, so neither goes through `receiptLabel`. The shadow-authority
 * flag, armed count, loss hold, and refusal code that used to sit beside the
 * mode word live in the same two places now — never dropped, just no longer
 * always on screen. Siblings come from injecting `FleetDirectoryService`
 * directly (`lanesOf(lane.broker)`), not a prop the caller must remember to
 * pass — a badge mounted deep inside a feature page gets correct
 * disambiguation for free, the same as the shell header.
 */
@Component({
  selector: 'app-alpaca-live-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [NgTemplateOutlet, RouterLink],
  styles: [`
    :host { display: contents; }
    .alpaca-banner__link {
      display: inline-flex; text-decoration: none; border-radius: var(--radius-pill);
    }
    .alpaca-banner__link:focus-visible {
      outline: 2px solid var(--p-primary-color, #6da2ff); outline-offset: 2px;
    }
    .alpaca-banner {
      display: inline-flex; min-height: 30px; align-items: center; gap: 0.3rem;
      padding: 0 0.55rem; border-radius: var(--radius-pill);
      font-size: var(--fs-xs); line-height: 1.2; white-space: nowrap;
      border: 1px solid rgba(178, 181, 190, 0.45); color: var(--text-primary);
      background: rgba(5, 8, 14, 0.42); font-variant-numeric: tabular-nums;
      transition: box-shadow 0.12s ease, border-color 0.12s ease;
    }
    .alpaca-banner.is-paper { color: #b9edff; border-color: #45b9e1; }
    .alpaca-banner.is-live-unarmed { color: #ffd0cf; border-color: #f06b68; font-weight: 650; }
    .alpaca-banner.is-live-armed { color: #fff; border-color: #ff8b88; background: rgba(90, 12, 20, 0.72); font-weight: 750; }
    .alpaca-banner.is-undetermined { color: #fff; border-color: #ffb020; background: rgba(122, 61, 0, 0.85); font-weight: 750; }
    .alpaca-banner.is-active {
      border-width: 2px;
      box-shadow: inset 0 0 0 1px var(--p-primary-color, #6da2ff);
    }
    .alpaca-banner__mode { font-weight: 750; }
    .alpaca-banner__lane { font-weight: 500; opacity: 0.7; }
  `],
  template: `
    @let b = badge();
    <ng-template #chip>
      <div
        class="alpaca-banner"
        [class]="b.tone"
        [class.is-active]="isActive()"
        role="status"
        [attr.aria-label]="b.ariaLabel"
        [attr.title]="b.detail"
      >
        <span class="alpaca-banner__mode">{{ b.mode }}</span>
        @if (b.laneHint) {
          <span class="alpaca-banner__lane">· {{ b.laneHint }}</span>
        }
      </div>
    </ng-template>

    @if (destination(); as link) {
      <a
        class="alpaca-banner__link"
        [routerLink]="link.commands"
        [queryParams]="link.queryParams"
      >
        <ng-container [ngTemplateOutlet]="chip" />
      </a>
    } @else {
      <ng-container [ngTemplateOutlet]="chip" />
    }
  `,
})
export class AlpacaLiveBannerComponent {
  private readonly service = inject(AlpacaLiveVerdictService);
  private readonly fleetDirectory = inject(FleetDirectoryService);
  private readonly currentUrl = inject(CurrentUrlService).url;

  /** The lane this badge speaks for, or `null` when the directory has not
   * produced one. `null` is a rendered state, not an absent input. */
  readonly lane = input.required<LaneDescriptor | null>();

  /**
   * Where this badge leads (ADR 0064 Decision 3): this account, on the tab
   * the operator is already standing on when they are inside a workspace,
   * otherwise its Overview. The resolver decides — a badge and the
   * workspace's own account switcher are the same move made from two places,
   * so they cannot land differently.
   *
   * `null` only for the roster-unknown badge: there is no lane to open, and a
   * link to nowhere would be worse than the warning standing on its own.
   */
  protected readonly destination = computed<AccountWorkspaceLink | null>(() => {
    const lane = this.lane();
    if (lane === null) return null;
    const url = this.currentUrl();
    const from = accountWorkspaceLocation(url);
    return accountWorkspaceBadgeRoute(
      from,
      {
        broker: lane.broker,
        clerkId: lane.clerk_id,
        accountId: laneConfirmedAccount(lane),
      },
      // A lens is a workspace's own perspective: outside one there is none to
      // keep, so a stray `?lens=` on some other page never rides along.
      from === null ? null : accountWorkspaceLens(url),
    );
  });

  /** Whether the operator is currently standing inside this lane's own
   * workspace — read-only, from the same URL `destination` already reads, so
   * the pressed pill and the link it sits in can never disagree about which
   * account is "here". */
  protected readonly isActive = computed<boolean>(() => {
    const lane = this.lane();
    if (lane === null) return false;
    const here = accountWorkspaceLocation(this.currentUrl());
    return here !== null && here.clerkId === lane.clerk_id;
  });

  protected readonly badge = computed<LaneBadge>(() => {
    const lane = this.lane();
    if (lane === null) {
      return finalizeBadge(undetermined({
        lane: null,
        laneHint: null,
        mode: 'Alpaca lanes unknown — assume real money',
        headline: 'The Alpaca lane directory has not resolved',
        detail: `The shell cannot list this browser's Alpaca lanes, so no lane's mode is known. ${ASSUME_REAL_MONEY}`,
      }));
    }

    const siblings = this.fleetDirectory.lanesOf(lane.broker);
    const { name, disambiguator } = laneDisplayName(lane, siblings);
    const state = this.service.stateFor(lane.clerk_id);
    const mode = modeWordFor(state);
    // Checked against what each sibling's pill actually reads right now, not
    // a raw lane count: with one paper and one live lane the words can never
    // collide, but a second live clerk on the same broker (ADR 0062 Phase 5)
    // makes two pills both read "Live" with nothing else to tell them apart
    // at a glance — the pill names which one it is again, the same
    // disambiguation the always-visible lane name used to give for free.
    const collides = siblings.some(
      (sibling) => sibling.clerk_id !== lane.clerk_id
        && modeWordFor(this.service.stateFor(sibling.clerk_id)) === mode,
    );
    const laneHint = collides ? laneDisplayNameText({ name, disambiguator }) : null;

    const { verdict, lastError } = state;
    if (verdict !== null) {
      return finalizeBadge(verdictBadge({ lane: name, disambiguator, laneHint, mode, verdict }));
    }
    if (lastError !== null) {
      return finalizeBadge(undetermined({
        lane: name,
        disambiguator,
        laneHint,
        mode,
        headline: 'Account verdict unavailable — the last read failed',
        detail: `The shell could not read this lane's Alpaca live verdict. ${ASSUME_REAL_MONEY}`,
      }));
    }
    return finalizeBadge(undetermined({
      lane: name,
      disambiguator,
      laneHint,
      mode,
      headline: 'Account verdict not yet read for this lane',
      detail: `No live-verdict read has completed for this lane yet. ${ASSUME_REAL_MONEY}`,
    }));
  });
}

/** Folds `shadow` / `armedCount` / `lossHold` / `refusalCode` into the
 * accessible name and the tooltip and drops them from the rendered type —
 * the one place those four facts turn into text, so trimming the pill's
 * visible content down to the mode word can never mean losing them; they
 * move to the hover/screen-reader surface instead. */
function finalizeBadge(raw: LaneBadgeFacts): LaneBadge {
  const parts: string[] = [];
  if (raw.shadow) parts.push('Shadow authority');
  if (raw.armedCount !== null) parts.push(`${raw.armedCount} armed`);
  if (raw.lossHold) parts.push('loss hold');
  if (raw.refusalCode) parts.push(formatReceiptLabel(raw.refusalCode));

  const { tone, mode, laneHint, ariaLabel, detail } = raw;
  if (parts.length === 0) return { tone, mode, laneHint, ariaLabel, detail };

  const extras = parts.join(' · ');
  return {
    tone,
    mode,
    laneHint,
    ariaLabel: `${ariaLabel} — ${extras}.`,
    detail: `${detail} ${extras}.`,
  };
}

/**
 * The pill's own visible mode word for one lane's current verdict-read
 * state — the single place "Live" / "Paper" / an undetermined sentence gets
 * derived, so `badge` can also ask it about siblings (to decide whether
 * `laneHint` is needed) without a second copy of this mapping to drift from
 * the first.
 *
 * "Paper" is deliberately shorter than `verdictModeChip`'s "Paper money"
 * (alpaca-live-verdict.service.ts) — see this file's class docstring for why
 * that split is intentional rather than the #2185 kind of drift.
 */
function modeWordFor(state: LaneVerdictState): string {
  if (state.verdict !== null) {
    switch (state.verdict.final_verdict) {
      case 'paper': return 'Paper';
      case 'live-unarmed':
      case 'live-armed': return 'Live';
      case 'unknown': return 'Mode unknown — assume real money';
    }
  }
  return state.lastError !== null
    ? 'Mode unavailable — assume real money'
    : 'Mode not yet read — assume real money';
}

function verdictBadge({
  lane,
  disambiguator,
  laneHint,
  mode,
  verdict: v,
}: {
  readonly lane: string;
  readonly disambiguator: string | null;
  readonly laneHint: string | null;
  readonly mode: string;
  readonly verdict: AlpacaLiveVerdict;
}): LaneBadgeFacts {
  // A shared display name is a supported state (ADR 0064 Decision 5), not an
  // edge case — the accessible name must carry the same disambiguator the
  // pill's own `laneHint` renders once there are enough lanes for it to
  // matter, or two colliding lanes announce identically to a screen reader.
  const accessibleName = laneDisplayNameText({ name: lane, disambiguator });
  const base = {
    laneHint,
    mode,
    ariaLabel: `${accessibleName}: ${v.headline}`,
    detail: v.detail,
    lossHold: v.loss_hold === 'held',
    // Derived once, before the switch, on exactly the same server field
    // `verdictModeChip` reads — so this badge and the lane's own mode chip
    // can never disagree about the authority the operator is standing in.
    shadow: v.clerk_authority === 'shadow',
  };
  switch (v.final_verdict) {
    case 'paper':
      return {
        ...base,
        tone: 'is-paper',
        armedCount: null,
        refusalCode: null,
      };
    case 'live-unarmed':
    case 'live-armed':
      return {
        ...base,
        tone: v.final_verdict === 'live-armed' ? 'is-live-armed' : 'is-live-unarmed',
        armedCount: v.armed_instance_count,
        refusalCode: null,
      };
    case 'unknown':
      return {
        ...undetermined({
          lane,
          disambiguator,
          laneHint,
          mode,
          // The server's own sentence survives in the accessible name and the
          // tooltip; the closed `mode` word above is what the chip shows.
          headline: v.headline,
          detail: v.detail,
          refusalCode: v.clerk_refusal_reason_code,
        }),
        lossHold: base.lossHold,
        shadow: base.shadow,
      };
  }
}

/** The one loud treatment every undetermined cause renders through. Only the
 * sentence differs; the tone, the visible assumption, and the accessible name
 * carrying that assumption do not. */
function undetermined({
  lane,
  disambiguator = null,
  laneHint = null,
  mode,
  headline,
  detail,
  refusalCode = null,
}: {
  /** Null only when the roster itself is the undetermined thing. */
  readonly lane: string | null;
  /** This lane's own label, folded into the accessible name only when the
   * name is shared. */
  readonly disambiguator?: string | null;
  readonly laneHint?: string | null;
  /** The chip's own visible sentence. */
  readonly mode: string;
  /** What the accessible name says happened. */
  readonly headline: string;
  /** Tooltip prose. */
  readonly detail: string;
  readonly refusalCode?: string | null;
}): LaneBadgeFacts {
  // Same reasoning as `verdictBadge`: the accessible name carries the
  // disambiguator too, not just the pill's own `laneHint`.
  const accessibleName = lane === null ? null : laneDisplayNameText({ name: lane, disambiguator });
  const named = accessibleName === null ? headline : `${accessibleName}: ${headline}`;
  return {
    tone: 'is-undetermined',
    laneHint,
    mode,
    ariaLabel: `${named}. ${ASSUME_REAL_MONEY}`,
    detail,
    // No verdict reached this path in three of its four causes, so no
    // authority is known to report. The `unknown` verdict case overrides this
    // with the authority its own read did carry.
    shadow: false,
    armedCount: null,
    lossHold: false,
    refusalCode,
  };
}
