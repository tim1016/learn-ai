import { ApplicationConfig, inject, provideZonelessChangeDetection } from "@angular/core";
import { provideRouter, withInMemoryScrolling, withExperimentalAutoCleanupInjectors } from "@angular/router";
import { provideHttpClient } from "@angular/common/http";
import { provideAnimationsAsync } from "@angular/platform-browser/animations/async";
import { providePrimeNG } from "primeng/config";
import { MessageService } from "primeng/api";
import { provideApollo } from "apollo-angular";
import { HttpLink } from "apollo-angular/http";
import { InMemoryCache } from "@apollo/client/core";
import Aura from "@primeuix/themes/aura";
import { environment } from "../environments/environment";
import { routes } from "./app.routes";

export const appConfig: ApplicationConfig = {
  providers: [
    provideZonelessChangeDetection(),
    provideRouter(
      routes,
      withInMemoryScrolling({ anchorScrolling: 'enabled', scrollPositionRestoration: 'enabled' }),
      withExperimentalAutoCleanupInjectors(),
    ),
    provideHttpClient(),
    provideAnimationsAsync(),
    providePrimeNG({
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
