// Production environment configuration
// WARNING: Never commit API keys to version control
// In production, use backend proxy instead of direct API calls
export const environment = {
  production: true,
  polygonApiKey: 'Z_ENWpm0RRrvXvLOY65uMH8RB93B4gBN', // Leave empty - use backend proxy in production
  useBackendProxy: true, // Always use backend in production
  backendUrl: 'http://localhost:5000/graphql',
  polygonProxyUrl: 'http://localhost:5000/api/polygon' // Backend proxy endpoint
};
