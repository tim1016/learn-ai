import { ChangeDetectionStrategy, Component, computed, input, output } from "@angular/core";
import { AssetIdentityComponent } from "../../../../shared/asset-identity/asset-identity.component";
import { TimestampDisplayComponent } from "../../../../shared/timestamp/timestamp-display.component";
import type { RecencySwimlaneTrade } from "../recency-swimlane/recency-swimlane-layout";

interface ParsedParam {
  name: string;
  value: unknown;
}

function parseParams(paramsJson: string | undefined): ParsedParam[] | null {
  if (!paramsJson) return null;
  try {
    const parsed: unknown = JSON.parse(paramsJson);
    if (typeof parsed !== "object" || parsed === null) return null;
    return Object.entries(parsed as Record<string, unknown>).map(([name, value]) => ({ name, value }));
  } catch {
    // Explicit, deliberate degrade-to-null (rendered as "parameters
    // unavailable") for malformed JSON — not a silent swallow, this IS
    // the handling.
    return null;
  }
}

/**
 * Pinned-trade focus panel (design spec D9, D24): shown once a swimlane
 * bar is clicked. Reports the trade's combo, entry/exit, holding period,
 * PnL, a link to the underlying study, and an explicit in-sample/OOS
 * caption — every field is Python-authored data passed through as-is.
 * The on-demand price sparkline (design spec §4, "Further Notes") is
 * deferred: it needs a Polygon-backed price-history endpoint that does
 * not exist yet, which is separate scope from this panel's own render.
 */
@Component({
  selector: "app-recency-trade-focus",
  standalone: true,
  imports: [AssetIdentityComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: "./recency-trade-focus.component.html",
  styleUrls: ["./recency-trade-focus.component.scss"],
})
export class RecencyTradeFocusComponent {
  readonly trade = input<RecencySwimlaneTrade | null>(null);
  readonly deleteRequested = output<number>();

  readonly params = computed<ParsedParam[] | null>(() => parseParams(this.trade()?.paramsJson));

  readonly pnlLabel = computed(() => {
    const t = this.trade();
    if (!t) return "";
    return `${t.pnl >= 0 ? "+" : ""}${t.pnl.toFixed(2)}`;
  });

  readonly durationLabel = computed(() => {
    const t = this.trade();
    if (!t) return "";
    return `${t.holdingSessions} session${t.holdingSessions === 1 ? "" : "s"}`;
  });

  readonly studyUrl = computed(() => {
    const studyId = this.trade()?.studyId;
    return studyId === null || studyId === undefined ? null : `/research-lab/strategy-runs/${studyId}`;
  });

  requestDelete(): void {
    const t = this.trade();
    if (t) this.deleteRequested.emit(t.recencyRunId);
  }
}
