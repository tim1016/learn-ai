import { Injectable, signal } from '@angular/core';

import { environment } from '../../../../environments/environment';

@Injectable({ providedIn: 'root' })
export class BrokerInstancesV2FlagService {
  private readonly _enabled = signal(environment.flags.brokerInstancesV2);

  readonly enabled = this._enabled.asReadonly();

  setEnabled(value: boolean): void {
    this._enabled.set(value);
  }
}
