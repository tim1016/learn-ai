import { Injectable, inject, resource } from '@angular/core';

import { BrokersService } from '../../../services/brokers.service';

/** One account read shared by the desk header and its active lens. */
@Injectable()
export class AlpacaDeskAccountDataService {
  private readonly brokers = inject(BrokersService);

  readonly account = resource({
    loader: () => this.brokers.getAccount('alpaca'),
  });
}
