import type { HttpInterceptorFn, HttpRequest } from "@angular/common/http";
import dataPlaneControlSurfaces from "@repo-contracts/data-plane-control-surfaces.json";

export const DATA_PLANE_CONTROL_INTENT_HEADER = "X-Data-Plane-Control-Intent";
export const DATA_PLANE_CONTROL_INTENT_QUERY = "control_intent";
export const DATA_PLANE_CONTROL_INTENT_VALUE = "learn-ai-browser-control";
export const DATA_PLANE_CONTROL_PREFIXES = dataPlaneControlSurfaces.control_prefixes;
export const DATA_PLANE_CONTROL_PROTECTED_READ_PREFIXES =
  dataPlaneControlSurfaces.protected_read_prefixes;

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);
const SAFE_READ_METHODS = new Set(["GET", "HEAD"]);

export const dataPlaneControlIntentInterceptor: HttpInterceptorFn = (request, next) => {
  if (!requiresDataPlaneControlIntent(request)) return next(request);
  return next(request.clone({
    setHeaders: {
      [DATA_PLANE_CONTROL_INTENT_HEADER]: DATA_PLANE_CONTROL_INTENT_VALUE,
    },
  }));
};

function requiresDataPlaneControlIntent(request: HttpRequest<unknown>): boolean {
  const path = requestPath(request.url);
  const method = request.method.toUpperCase();
  if (UNSAFE_METHODS.has(method)) return matchesAnyPrefix(path, DATA_PLANE_CONTROL_PREFIXES);
  if (SAFE_READ_METHODS.has(method)) {
    return matchesAnyPrefix(path, DATA_PLANE_CONTROL_PROTECTED_READ_PREFIXES);
  }
  return false;
}

function requestPath(url: string): string {
  if (!/^https?:\/\//i.test(url)) return url.split("?")[0] ?? url;
  try {
    return new URL(url).pathname;
  } catch {
    return url;
  }
}

function matchesAnyPrefix(path: string, prefixes: readonly string[]): boolean {
  return prefixes.some((prefix) => path === prefix || path.startsWith(`${prefix}/`));
}
