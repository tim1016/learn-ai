Verify all containers are healthy and services are responding.

1. **Container status**:
   ```bash
   podman compose ps
   ```

2. **Service health** (check each endpoint):
   - Frontend: `curl -sf http://localhost:4200 > /dev/null && echo "OK" || echo "FAIL"`
   - Python: `curl -sf http://localhost:8000/docs > /dev/null && echo "OK" || echo "FAIL"`
   - Postgres: `podman exec my-postgres pg_isready -U postgres`
   - Redis: `podman exec my-redis sh -c 'redis-cli --no-auth-warning -a "$REDIS_PASSWORD" ping'`

3. **Container logs** (last 5 lines each, check for errors):
   ```bash
   podman logs --tail 5 my-frontend
   podman logs --tail 5 polygon-data-service
   podman logs --tail 5 my-postgres
   podman logs --tail 5 my-redis
   ```

Report a summary table: service, container status, endpoint status, any errors in logs.
