import { ChangeDetectionStrategy, Component, inject } from '@angular/core';

import { AssetIdentityComponent } from '../asset-identity/asset-identity.component';
import { TimestampDisplayComponent } from '../timestamp/timestamp-display.component';
import { OptionArchiveService } from './option-archive.service';

@Component({
  selector: 'app-option-archive-card',
  imports: [AssetIdentityComponent, TimestampDisplayComponent],
  changeDetection: ChangeDetectionStrategy.OnPush,
  templateUrl: './option-archive-card.component.html',
  styleUrl: './option-archive-card.component.scss',
})
export class OptionArchiveCardComponent {
  readonly archive = inject(OptionArchiveService);
}
