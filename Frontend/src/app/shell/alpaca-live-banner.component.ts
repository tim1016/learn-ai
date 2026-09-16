import { ChangeDetectionStrategy, Component, computed, inject, input } from '@angular/core';

import type { AlpacaLiveVerdict } from '../api/alpaca.types';
import { FleetDirectoryService } from '../fleet/fleet-directory.service';
import { laneDisplayName, laneDisplayNameText, type LaneDescriptor } from '../fleet/fleet-directory.types';
import { AlpacaLiveVerdictService } from '../services/alpaca-live-verdict.service';
import { ReceiptLabelPipe } from '../shared/pipes/receipt-label.pipe';

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
 * One rendered badge. Built once per change in TypeScript so the closed
 * `final_verdict` union is mapped exhaustively in one place: a fifth verdict
 * added to the contract is a compile error here, rather than warning *text*
 * silently paired with calm *styling* in a template `@else`.
 */
interface LaneBadge {
  readonly tone: BadgeTone;
  /** The lane's display name (nickname, or its label until one is set), or
   * null when no lane can be named because the directory itself has not
   * resolved. */
  readonly lane: string | null;
  /** This lane's own label, shown beside `lane` only when that display name
   * is shared with another lane in the directory (ADR 0064 Decision 5). */
  readonly disambiguator: string | null;
  readonly mode: string;
  readonly ariaLabel: string;
  readonly detail: string;
  readonly account: string | null;
  readonly armedCount: number | null;
  readonly lossHold: boolean;
  readonly refusalCode: string | null;
}

/**
 * Compact Alpaca account-mode trust anchor for ONE lane (ADR 0059 D8;
 * ADR 0011; #2110). Renders exactly the lane it is given — the shell mounts
 * one instance per `FleetDirectoryService.lanesOf('alpaca')` entry, never a
 * merged or hardcoded pair, so every badge carries its own lane label.
 *
 * The server verdict remains the only truth source. Live mode keeps the
 * account id and armed count visible even in the dense global header.
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
 * Note: the badge's own name is `laneDisplayName` (fleet-directory.types) —
 * the lane's account nickname, or its `display_label` (set once at clerk
 * enrolment via `provision --label` / `migrate-existing --label`) until one
 * is set. Both are operator prose, not backend identifiers, so neither goes
 * through `receiptLabel`. A name shared with another lane of the same broker
 * shows that lane's own label beside it (ADR 0064 Decision 5); nothing here
 * refuses the duplicate. Siblings come from injecting `FleetDirectoryService`
 * directly (`lanesOf(lane.broker)`), not a prop the caller must remember to
 * pass — a `LaneContextStripComponent` mounted deep in the Bots/Gallery pages
 * gets correct disambiguation for free, the same as the shell header.
 */
@Component({
  selector: 'app-alpaca-live-banner',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [ReceiptLabelPipe],
  styles: [`
    :host { display: contents; }
    .alpaca-banner {
      display: inline-flex; min-height: 30px; align-items: center; gap: 0.35rem;
      padding: 0 0.65rem; border-radius: var(--radius-pill);
      font-size: var(--fs-xs); line-height: 1.2; white-space: nowrap;
      border: 1px solid rgba(178, 181, 190, 0.45); color: var(--text-primary);
      background: rgba(5, 8, 14, 0.42); font-variant-numeric: tabular-nums;
    }
    .alpaca-banner.is-paper { color: #b9edff; border-color: #45b9e1; }
    .alpaca-banner.is-live-unarmed { color: #ffd0cf; border-color: #f06b68; font-weight: 650; }
    .alpaca-banner.is-live-armed { color: #fff; border-color: #ff8b88; background: rgba(90, 12, 20, 0.72); font-weight: 750; }
    .alpaca-banner.is-undetermined { color: #fff; border-color: #ffb020; background: rgba(122, 61, 0, 0.85); font-weight: 750; }
    .alpaca-banner__lane { font-weight: 750; opacity: 0.8; }
    .alpaca-banner__disambiguator { font-weight: 500; opacity: 0.65; }
    .alpaca-banner__mode { font-weight: 750; }
    .alpaca-banner__detail { opacity: 0.86; }
    .alpaca-banner__hold { font-weight: 750; color: #ffaaa8; }
  `],
  template: `
    @let b = badge();
    <div
      class="alpaca-banner"
      [class]="b.tone"
      role="status"
      [attr.aria-label]="b.ariaLabel"
      [attr.title]="b.detail"
    >
      @if (b.lane) {
        <span class="alpaca-banner__lane">{{ b.lane }}</span>
        @if (b.disambiguator; as disambiguator) {
          <span class="alpaca-banner__disambiguator">({{ disambiguator }})</span>
        }
      }
      <span class="alpaca-banner__mode">{{ b.mode }}</span>
      @if (b.account) {
        <span class="alpaca-banner__detail">· {{ b.account }}</span>
        <span class="alpaca-banner__detail">· {{ b.armedCount }} armed</span>
      }
      @if (b.lossHold) {
        <span class="alpaca-banner__hold">· loss hold</span>
      }
      @if (b.refusalCode) {
        <span>· {{ b.refusalCode | receiptLabel }}</span>
      }
    </div>
  `,
})
export class AlpacaLiveBannerComponent {
  private readonly service = inject(AlpacaLiveVerdictService);
  private readonly fleetDirectory = inject(FleetDirectoryService);

  /** The lane this badge speaks for, or `null` when the directory has not
   * produced one. `null` is a rendered state, not an absent input. */
  readonly lane = input.required<LaneDescriptor | null>();

  protected readonly badge = computed<LaneBadge>(() => {
    const lane = this.lane();
    if (lane === null) {
      return undetermined({
        lane: null,
        mode: 'Alpaca lanes unknown — assume real money',
        headline: 'The Alpaca lane directory has not resolved',
        detail: `The shell cannot list this browser's Alpaca lanes, so no lane's mode is known. ${ASSUME_REAL_MONEY}`,
      });
    }

    const { name, disambiguator } = laneDisplayName(lane, this.fleetDirectory.lanesOf(lane.broker));
    const { verdict, lastError } = this.service.stateFor(lane.clerk_id);
    if (verdict !== null) return verdictBadge(name, disambiguator, verdict);
    if (lastError !== null) {
      return undetermined({
        lane: name,
        disambiguator,
        mode: 'Mode unavailable — assume real money',
        headline: 'Account verdict unavailable — the last read failed',
        detail: `The shell could not read this lane's Alpaca live verdict. ${ASSUME_REAL_MONEY}`,
      });
    }
    return undetermined({
      lane: name,
      disambiguator,
      mode: 'Mode not yet read — assume real money',
      headline: 'Account verdict not yet read for this lane',
      detail: `No live-verdict read has completed for this lane yet. ${ASSUME_REAL_MONEY}`,
    });
  });
}

function verdictBadge(
  lane: string,
  disambiguator: string | null,
  v: AlpacaLiveVerdict,
): LaneBadge {
  // A shared display name is a supported state (ADR 0064 Decision 5), not an
  // edge case — the accessible name must carry the same "(Label)"
  // disambiguator the visible pill renders, or two colliding lanes announce
  // identically to a screen reader.
  const accessibleName = laneDisplayNameText({ name: lane, disambiguator });
  const base = {
    lane,
    disambiguator,
    ariaLabel: `${accessibleName}: ${v.headline}`,
    detail: v.detail,
    lossHold: v.loss_hold === 'held',
  };
  switch (v.final_verdict) {
    case 'paper':
      return {
        ...base,
        tone: 'is-paper',
        mode: 'Paper money',
        account: null,
        armedCount: null,
        refusalCode: null,
      };
    case 'live-unarmed':
    case 'live-armed':
      return {
        ...base,
        tone: v.final_verdict === 'live-armed' ? 'is-live-armed' : 'is-live-unarmed',
        mode: 'Live',
        account: v.observed_account_id ?? 'account unknown',
        armedCount: v.armed_instance_count,
        refusalCode: null,
      };
    case 'unknown':
      return {
        ...undetermined({
          lane,
          disambiguator,
          mode: 'Mode unknown — assume real money',
          // The server's own sentence survives in the accessible name and the
          // tooltip; the closed `mode` copy above is what the chip shows.
          headline: v.headline,
          detail: v.detail,
          refusalCode: v.clerk_refusal_reason_code,
        }),
        lossHold: base.lossHold,
      };
  }
}

/** The one loud treatment every undetermined cause renders through. Only the
 * sentence differs; the tone, the visible assumption, and the accessible name
 * carrying that assumption do not. */
function undetermined({
  lane,
  disambiguator = null,
  mode,
  headline,
  detail,
  refusalCode = null,
}: {
  /** Null only when the roster itself is the undetermined thing. */
  readonly lane: string | null;
  /** This lane's own label, shown beside `lane` only when the name is shared. */
  readonly disambiguator?: string | null;
  /** The chip's own visible sentence. */
  readonly mode: string;
  /** What the accessible name says happened. */
  readonly headline: string;
  /** Tooltip prose. */
  readonly detail: string;
  readonly refusalCode?: string | null;
}): LaneBadge {
  // Same reasoning as `verdictBadge`: the accessible name carries the
  // disambiguator too, not just the visible `lane` span.
  const accessibleName = lane === null ? null : laneDisplayNameText({ name: lane, disambiguator });
  const named = accessibleName === null ? headline : `${accessibleName}: ${headline}`;
  return {
    tone: 'is-undetermined',
    lane,
    disambiguator,
    mode,
    ariaLabel: `${named}. ${ASSUME_REAL_MONEY}`,
    detail,
    account: null,
    armedCount: null,
    lossHold: false,
    refusalCode,
  };
}
