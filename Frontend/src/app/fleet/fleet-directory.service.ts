/** The broker-neutral clerk fleet directory (PRD §10.1/FR-093).
 *
 * One root service loads `/api/broker-clerks` and exposes lanes keyed by
 * `(broker, clerk_id)`. Every lane-scoped consumer derives its own state
 * from the descriptor it reads here: one failed lane's error never bleeds
 * into another lane's resources, and a deep link resolves its clerk here —
 * failing in place when unknown (FR-096), never redirecting to a different
 * lane. */

import { HttpClient } from '@angular/common/http';
import { Injectable, inject, signal } from '@angular/core';
import { firstValueFrom } from 'rxjs';

import {
  FleetDirectoryResponse,
  LaneDescriptor,
  laneConfirmedAccount,
} from './fleet-directory.types';

@Injectable({ providedIn: 'root' })
export class FleetDirectoryService {
  private readonly http = inject(HttpClient);

  private readonly response = signal<FleetDirectoryResponse | undefined>(undefined);
  private readonly loadError = signal<unknown | undefined>(undefined);
  private readonly loading = signal(false);
  private inFlight: Promise<void> | null = null;

  /** The directory response, or undefined while loading / after an error. */
  readonly value = this.response.asReadonly();

  readonly error = this.loadError.asReadonly();

  readonly isLoading = this.loading.asReadonly();

  constructor() {
    // The directory is a rendered root resource. `load` stores an error as
    // observable state; a caller that needs a routing decision receives it
    // from ensureLoaded without requiring a second request.
    void this.load();
  }

  /** All lanes of one broker, in directory order (Paper next to Live). */
  readonly lanesOf = (broker: string): LaneDescriptor[] =>
    (this.response()?.clerks ?? []).filter((lane) => lane.broker === broker);

  lane(broker: string, clerkId: string): LaneDescriptor | undefined {
    return this.lanesOf(broker).find((lane) => lane.clerk_id === clerkId);
  }

  /** The lane whose confirmed binding serves this account, if any. */
  laneForAccount(broker: string, accountId: string): LaneDescriptor | undefined {
    const canonical = accountId.trim().toLowerCase();
    return this.lanesOf(broker).find(
      (lane) => laneConfirmedAccount(lane)?.toLowerCase() === canonical,
    );
  }

  refresh(): Promise<FleetDirectoryResponse> {
    return this.awaitLoaded(this.load());
  }

  /** Await one directory load — redirect guards need the resolved lanes
   * before deciding where an unscoped URL lands. */
  ensureLoaded(): Promise<FleetDirectoryResponse> {
    const response = this.response();
    if (response !== undefined) return Promise.resolve(response);
    const error = this.loadError();
    return error === undefined ? this.awaitLoaded(this.load()) : Promise.reject(error);
  }

  /** One in-flight operation owns both state and every awaiting caller. */
  private load(): Promise<void> {
    if (this.inFlight !== null) return this.inFlight;

    this.loading.set(true);
    this.loadError.set(undefined);
    const request = firstValueFrom(this.http.get<FleetDirectoryResponse>('/api/broker-clerks'));
    this.inFlight = request.then(
      (response) => {
        this.response.set(response);
      },
      (error: unknown) => {
        this.loadError.set(error);
      },
    ).finally(() => {
      this.loading.set(false);
      this.inFlight = null;
    });
    return this.inFlight;
  }

  private async awaitLoaded(load: Promise<void>): Promise<FleetDirectoryResponse> {
    await load;
    const error = this.loadError();
    if (error !== undefined) throw error;
    const response = this.response();
    if (response === undefined) throw new Error('Fleet directory load completed without a response.');
    return response;
  }
}
