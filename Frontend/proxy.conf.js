const DATA_PLANE_CONTROL_SECRET_HEADER = 'X-Data-Plane-Control-Secret';
const DATA_PLANE_CONTROL_INTENT_HEADER = 'X-Data-Plane-Control-Intent';
const DATA_PLANE_CONTROL_INTENT_QUERY = 'control_intent';
const DATA_PLANE_CONTROL_INTENT_VALUE = 'learn-ai-browser-control';
const dataPlaneControlSurfaces = require('../contracts/data-plane-control-surfaces.json');
// Host development reaches the compose services through their loopback ports.
// Containers override these targets with their compose-network service names.
// Keeping the control-header hook in this one configuration prevents a local
// target override from accidentally bypassing data-plane authorization.
const backendProxyTarget = process.env.BACKEND_PROXY_TARGET ?? 'http://127.0.0.1:5000';
const DEFAULT_DATA_PLANE_PROXY_TARGET = 'http://127.0.0.1:8000';
const TRUSTED_DATA_PLANE_PROXY_HOSTS = new Set(['127.0.0.1', 'localhost', 'python-service']);
const dataPlaneControlSecret = process.env.DATA_PLANE_CONTROL_SECRET ?? 'local-dev-control-secret';
const UNSAFE_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);
const SAFE_READ_METHODS = new Set(['GET', 'HEAD']);
const CONTROL_PREFIXES = dataPlaneControlSurfaces.control_prefixes;
const PROTECTED_READ_PREFIXES = dataPlaneControlSurfaces.protected_read_prefixes;
const LOCAL_DEV_ORIGINS = new Set([
  'http://localhost:4200',
  'http://127.0.0.1:4200',
]);

function resolveDataPlaneProxyTarget(value) {
  const target = value?.trim();
  if (!target) {
    throw new Error('DATA_PLANE_PROXY_TARGET must be a trusted local or Compose service URL.');
  }
  let parsed;
  try {
    parsed = new URL(target);
  } catch {
    throw new Error('DATA_PLANE_PROXY_TARGET must be a valid trusted local or Compose service URL.');
  }
  if (
    parsed.protocol !== 'http:'
    || !TRUSTED_DATA_PLANE_PROXY_HOSTS.has(parsed.hostname)
    || parsed.username
    || parsed.password
    || parsed.pathname !== '/'
    || parsed.search
    || parsed.hash
  ) {
    throw new Error('DATA_PLANE_PROXY_TARGET must be a trusted local or Compose service URL.');
  }
  return parsed.origin;
}

const dataPlaneProxyTarget = resolveDataPlaneProxyTarget(
  process.env.DATA_PLANE_PROXY_TARGET ?? DEFAULT_DATA_PLANE_PROXY_TARGET,
);

function requestHeader(req, name) {
  const value = req.headers[name.toLowerCase()];
  return Array.isArray(value) ? value[0] : value;
}

function requestPath(req) {
  const url = req.url ?? '';
  const path = url.startsWith('/api') ? url : `/api${url.startsWith('/') ? url : `/${url}`}`;
  return path.split('?')[0];
}

function requestQueryParam(req, name) {
  const url = req.url ?? '';
  const queryIndex = url.indexOf('?');
  if (queryIndex < 0) return undefined;
  const value = new URLSearchParams(url.slice(queryIndex + 1)).get(name);
  return value === null ? undefined : value;
}

function requestControlIntent(req) {
  return requestHeader(req, DATA_PLANE_CONTROL_INTENT_HEADER);
}

function requestProtectedControlReadIntent(req) {
  return requestControlIntent(req)
    ?? requestQueryParam(req, DATA_PLANE_CONTROL_INTENT_QUERY);
}

function matchesAnyPrefix(path, prefixes) {
  return prefixes.some((prefix) => path === prefix || path.startsWith(`${prefix}/`));
}

function isControlMutation(req) {
  if (!UNSAFE_METHODS.has((req.method ?? '').toUpperCase())) return false;
  return matchesAnyPrefix(requestPath(req), CONTROL_PREFIXES);
}

function isProtectedControlRead(req) {
  if (!SAFE_READ_METHODS.has((req.method ?? '').toUpperCase())) return false;
  return matchesAnyPrefix(requestPath(req), PROTECTED_READ_PREFIXES);
}

function requiresDataPlaneControlSecret(req) {
  return isControlMutation(req) || isProtectedControlRead(req);
}

function isLocalDevUrl(value) {
  if (!value) return false;
  try {
    const parsed = new URL(value);
    return LOCAL_DEV_ORIGINS.has(parsed.origin);
  } catch {
    return false;
  }
}

function hasSameOriginFetchMetadata(req) {
  // Metadata-absent local tools do not receive the private proxy secret. The
  // Angular app sends the public intent header; browser provenance must still
  // prove this is same-origin localhost UI traffic before we attach the secret.
  const secFetchSite = requestHeader(req, 'sec-fetch-site');
  if (secFetchSite !== 'same-origin' && secFetchSite !== 'none') return false;
  const origin = requestHeader(req, 'origin');
  const referer = requestHeader(req, 'referer');
  if (origin && !isLocalDevUrl(origin)) return false;
  if (referer && !isLocalDevUrl(referer)) return false;
  return Boolean(origin || referer);
}

function shouldAttachDataPlaneSecret(req) {
  if (!dataPlaneControlSecret || !requiresDataPlaneControlSecret(req)) return false;
  const intent = isProtectedControlRead(req)
    ? requestProtectedControlReadIntent(req)
    : requestControlIntent(req);
  return (
    intent === DATA_PLANE_CONTROL_INTENT_VALUE
    && hasSameOriginFetchMetadata(req)
  );
}

function attachDataPlaneSecret(proxyReq, req) {
  if (shouldAttachDataPlaneSecret(req)) {
    proxyReq.setHeader(DATA_PLANE_CONTROL_SECRET_HEADER, dataPlaneControlSecret);
    return;
  }
  if (typeof proxyReq.removeHeader === 'function') {
    proxyReq.removeHeader(DATA_PLANE_CONTROL_SECRET_HEADER);
  }
}

function configureDataPlaneProxy(proxy) {
  proxy.on('proxyReq', attachDataPlaneSecret);
}

const proxyConfig = {
  '/graphql': {
    target: backendProxyTarget,
    secure: false,
    changeOrigin: true,
  },
  '/api/jobs': {
    target: backendProxyTarget,
    secure: false,
    changeOrigin: true,
  },
  '/api': {
    target: dataPlaneProxyTarget,
    secure: false,
    changeOrigin: true,
    configure: configureDataPlaneProxy,
  },
};

Object.defineProperty(proxyConfig, '__test', {
  enumerable: false,
  value: {
    DATA_PLANE_CONTROL_SECRET_HEADER,
    DATA_PLANE_CONTROL_INTENT_HEADER,
    DATA_PLANE_CONTROL_INTENT_QUERY,
    DATA_PLANE_CONTROL_INTENT_VALUE,
    backendProxyTarget,
    dataPlaneProxyTarget,
    dataPlaneControlSecret,
    CONTROL_PREFIXES,
    PROTECTED_READ_PREFIXES,
    attachDataPlaneSecret,
    configureDataPlaneProxy,
    isControlMutation,
    isProtectedControlRead,
    requiresDataPlaneControlSecret,
    shouldAttachDataPlaneSecret,
  },
});

module.exports = proxyConfig;
