import type { HttpInterceptorFn, HttpRequest } from "@angular/common/http";
import dataPlaneControlSurfaces from "@repo-contracts/data-plane-control-surfaces.json";

export const DATA_PLANE_CONTROL_INTENT_HEADER = "X-Data-Plane-Control-Intent";
export const DATA_PLANE_CONTROL_INTENT_VALUE = "learn-ai-browser-control";
export const DATA_PLANE_CONTROL_PREFIXES = dataPlaneControlSurfaces.control_prefixes;

const UNSAFE_METHODS = new Set(["POST", "PUT", "PATCH", "DELETE"]);

export const dataPlaneControlIntentInterceptor: HttpInterceptorFn = (request, next) => {
  if (!isDataPlaneControlMutation(request)) return next(request);
  return next(request.clone({
    setHeaders: {
      [DATA_PLANE_CONTROL_INTENT_HEADER]: DATA_PLANE_CONTROL_INTENT_VALUE,
    },
  }));
};

function isDataPlaneControlMutation(request: HttpRequest<unknown>): boolean {
  if (!UNSAFE_METHODS.has(request.method.toUpperCase())) return false;
  const path = requestPath(request.url);
  return DATA_PLANE_CONTROL_PREFIXES.some((prefix) => path === prefix || path.startsWith(`${prefix}/`));
}

function requestPath(url: string): string {
  if (!/^https?:\/\//i.test(url)) return url.split("?")[0] ?? url;
  try {
    return new URL(url).pathname;
  } catch {
    return url;
  }
}
