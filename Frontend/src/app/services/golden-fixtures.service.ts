import { HttpClient } from '@angular/common/http';
import { inject, Injectable } from '@angular/core';
import { firstValueFrom } from 'rxjs';
import { environment } from '../../environments/environment';
import type { GoldenFixturesCatalog } from './golden-fixtures.types';

@Injectable({ providedIn: 'root' })
export class GoldenFixturesService {
  private readonly http = inject(HttpClient);
  private readonly base = `${environment.pythonServiceUrl}/api`;

  async getCatalog(): Promise<GoldenFixturesCatalog> {
    return firstValueFrom(
      this.http.get<GoldenFixturesCatalog>(`${this.base}/golden-fixtures`),
    );
  }
}
