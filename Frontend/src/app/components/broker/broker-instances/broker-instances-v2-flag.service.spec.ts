import { TestBed } from '@angular/core/testing';
import { describe, it, expect } from 'vitest';

import { BrokerInstancesV2FlagService } from './broker-instances-v2-flag.service';

describe('BrokerInstancesV2FlagService', () => {
  it('defaults enabled() to false', () => {
    const service = TestBed.inject(BrokerInstancesV2FlagService);

    expect(service.enabled()).toBe(false);
  });
});
