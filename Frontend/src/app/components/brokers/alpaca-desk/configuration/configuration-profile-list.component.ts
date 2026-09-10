import { ChangeDetectionStrategy, Component, computed, input, output } from '@angular/core';

import type { BrokerInstallationSelection, BrokerProfile } from '../../../../api/alpaca.types';
import { TimestampDisplayComponent } from '../../../../shared/timestamp/timestamp-display.component';

/** One profile plus the roles this installation currently gives it. */
interface ProfileRow {
  readonly profile: BrokerProfile;
  readonly staged: boolean;
  readonly effective: boolean;
}

/**
 * The saved profiles, and the two things an operator does to a whole profile:
 * stage it, or archive it. Renaming, cloning and editing a revision all need
 * the profile open, so they live in the detail panel rather than as row
 * actions that would each need their own inline form here.
 */
@Component({
  selector: 'app-configuration-profile-list',
  changeDetection: ChangeDetectionStrategy.OnPush,
  imports: [TimestampDisplayComponent],
  templateUrl: './configuration-profile-list.component.html',
  styleUrl: './configuration-profile-list.component.scss',
  host: { class: 'block' },
})
export class ConfigurationProfileListComponent {
  readonly profiles = input.required<readonly BrokerProfile[]>();
  readonly selection = input.required<BrokerInstallationSelection | null>();
  readonly selectedProfileId = input<string | null>(null);
  readonly busy = input(false);

  readonly opened = output<string>();
  readonly staged = output<BrokerProfile>();
  readonly archiveToggled = output<{ profileId: string; archived: boolean }>();

  protected readonly rows = computed<readonly ProfileRow[]>(() => {
    const current = this.selection();
    return this.profiles().map((profile) => ({
      profile,
      staged: current?.staged_profile_id === profile.profile_id,
      effective: current?.effective_profile_id === profile.profile_id,
    }));
  });
}
