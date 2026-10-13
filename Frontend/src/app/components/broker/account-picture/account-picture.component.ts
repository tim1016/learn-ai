import { ChangeDetectionStrategy, Component, input } from '@angular/core';
import { AssetIdentityComponent } from '../../../shared/asset-identity/asset-identity.component';
import { AuthoredUsdPipe } from '../../../shared/pipes/authored-usd.pipe';
import { TimestampDisplayComponent } from '../../../shared/timestamp/timestamp-display.component';
import type { AccountPictureView } from '../v2-panel/lib/broker-v2-panel.service';
import { FigureRowComponent } from './figure-row.component';

let nextHeadingId = 0;

/**
 * What the account owns, what is spoken for, where it is concentrated and what it is obliged to (#3101).
 *
 * Purely presentational. Python authors every figure with its terms, source and age, the line each part
 * says, and the counts; this card lists them in that order, says a stale or unknown figure is one, and
 * adds and compares nothing, so the lines can never disagree with the cash they are the parts of. Each
 * figure opens into what it is made of, by bot where it is a bot's. It informs and blocks nothing.
 *
 * An expiry is a date, not an instant, and is shown as a date in the exchange's time (`date-et`).
 */
@Component({
  selector: 'app-account-picture',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [AssetIdentityComponent, AuthoredUsdPipe, FigureRowComponent, TimestampDisplayComponent],
  templateUrl: './account-picture.component.html',
  styleUrl: './account-picture.component.scss',
})
export class AccountPictureComponent {
  readonly picture = input.required<AccountPictureView>();

  protected readonly ids = {
    owned: `account-picture-owned-${nextHeadingId}`,
    spoken: `account-picture-spoken-${nextHeadingId}`,
    concentration: `account-picture-concentration-${nextHeadingId}`,
    obligations: `account-picture-obligations-${nextHeadingId++}`,
  };
}
