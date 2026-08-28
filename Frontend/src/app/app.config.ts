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
import { definePreset } from "@primeuix/themes";
import Aura from "@primeuix/themes/aura";
import { environment } from "../environments/environment";
import { routes } from "./app.routes";
import { dataPlaneControlIntentInterceptor } from "./security/data-plane-control-intent.interceptor";

/**
 * The menubar is shell chrome, not a card. It renders inside the top bar and
 * contributes no surface of its own, so the root is flattened and the label
 * scale is pulled down to the shell's densest step. Expressed as design tokens
 * rather than `::ng-deep` so the chrome stays stylable from one place.
 */
const ShellPreset = definePreset(Aura, {
  components: {
    menubar: {
      root: {
        background: "transparent",
        borderColor: "transparent",
        borderRadius: "0",
        padding: "0",
        gap: "0",
      },
      baseItem: {
        padding: "0.3rem 0.55rem",
      },
      item: {
        label: {
          fontSize: "var(--fs-xs)",
          fontWeight: "600",
        },
      },
    },
  },
});

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
        preset: ShellPreset,
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
