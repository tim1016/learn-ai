import { ApplicationConfig, inject, provideZonelessChangeDetection } from "@angular/core";
import {
  provideRouter,
  withComponentInputBinding,
  withExperimentalAutoCleanupInjectors,
  withInMemoryScrolling,
} from "@angular/router";
import { provideHttpClient, withInterceptors, withXhr } from "@angular/common/http";
import { providePrimeNG } from "primeng/config";
import { MessageService } from "primeng/api";
import { provideApollo } from "apollo-angular";
import { HttpLink } from "apollo-angular/http";
import { InMemoryCache } from "@apollo/client/core";
import Aura from "@primeuix/themes/aura";
import { environment } from "../environments/environment";
import { routes } from "./app.routes";
import { dataPlaneControlIntentInterceptor } from "./security/data-plane-control-intent.interceptor";

export const appConfig: ApplicationConfig = {
  providers: [
    provideZonelessChangeDetection(),
    provideRouter(
      routes,
      withComponentInputBinding(),
      withInMemoryScrolling({ anchorScrolling: 'enabled', scrollPositionRestoration: 'enabled' }),
      withExperimentalAutoCleanupInjectors(),
    ),
    provideHttpClient(withXhr(), withInterceptors([dataPlaneControlIntentInterceptor])),
    providePrimeNG({
      license: environment.primeUiLicense,
      theme: {
        preset: Aura,
        options: {
          darkModeSelector: '.app-dark',
        },
      },
    }),
    MessageService,
    provideApollo(() => ({
      link: inject(HttpLink).create({ uri: environment.backendUrl }),
      cache: new InMemoryCache(),
    })),
  ],
};
