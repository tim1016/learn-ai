# ADR 0003 — Operational topology: Windows host-venv preserved; container/VPS migration deferred with explicit triggers

**Status:** Accepted 2026-05-28
**Decision drivers:** persistent paper-trading bot survival across app closures and reboots; shadow strategy onboarding; design-doc proposal of `gnzsnz/ib-gateway-docker` superseded by operational reality.
**Related:** ADR 0001 (substrate), ADR 0002 (shadow mode), `docs/ibkr-integration-authority.md`, `docs/ibkr-paper-deployment-plan.md` § 16.

## Context

The pre-implementation design draft proposed the `gnzsnz/ib-gateway-docker` Docker bundle (Gateway + IBC + VNC, `restart: always`, `AUTO_RESTART_TIME`, `TRADING_MODE=paper`) supervised by Docker Compose alongside the existing services. The motivation was uniform supervision, mirrored deployment to a future VPS, and the bot's Python environment isolated from the developer machine.

Reconnaissance against `master` surfaced two facts that change the decision:

1. **IBKR error 420 forces co-location of bot and Gateway.** Per `ibkr-integration-authority.md` § 12 item 7 and the 2026-05-13 change-log entry (post PR #230): IBKR rejects `reqRealTimeBars` whenever the API client's source IP differs from the Gateway's login IP. The Podman / WSL bridge IP fails this check. Today's working operator path runs both the IB Gateway (native, with IBC) and the live-runtime `run.py start` process on the Windows host venv. Trusted IPs are `127.0.0.1` plus the polygon-data-service container's WSL bridge IP (for read-only diagnose endpoints only).
2. **Phase 10 — actual paper week with end-to-end `decisions.parquet` — has not yet been run successfully.** The longest live-Gateway session attempted was 30 min on 2026-05-13; indicator warmup is ≥ 3h 45m and has not completed in any test session. The writer-schema and reconcile-loader contract is therefore unverified against real artifacts. This is the gating operational event before Layer A divergence reporting has data to consume.

A full container migration before Phase 10 would re-do error-420 / Trusted-IPs / IBC-handshake hardening already shipped, for zero new capability. It would reorder the build queue badly: pay container-network costs before validating that the bot produces end-to-end artifacts at all.

## Decision

**(T3) Hybrid: Preserve today's Windows host-venv topology; explicit written triggers for container or VPS migration.**

Today's layout:
- **Windows host (native):** IB Gateway + IBC, logged into the paper account, Trusted IPs `127.0.0.1` + WSL bridge.
- **Windows host venv (managed by `host_daemon.py`):** the executing live-runtime process (`python -m app.engine.live.run start` per strategy spec), the shadow process (same CLI, `submit_mode = "shadow"` via strategy spec). One Gateway, multiple `clientId`s — each strategy spec pins its own.
- **Windows host (system service, net-new):** `host_daemon.py` wrapped in NSSM (or equivalent Windows Service) so it auto-starts on boot. This is the supervisor for both executing and shadow processes and the surface the FastAPI service talks to for lifecycle.
- **Podman compose:** observability / frontend / support services only (`my-postgres`, `my-redis`, `polygon-data-service`, `my-frontend`). The container surface reads bot artifacts via the `./PythonDataService/artifacts` bind mount; it does not own any IBKR session.

`host_daemon.py` extended from `_current: ManagedProcess | None` to `_managed: dict[strategy_instance_id, ManagedProcess]` to supervise the second process. This is the only net-new supervisor work the topology implies.

### Migration triggers (when (T3) is no longer enough)

Migrate from (T3) to (T4) — Linux VPS with `gnzsnz`-style Gateway-in-container and bots-in-container — when **any** of the following fires:

1. Laptop sleep / reboot / OS update causes a missed paper-trading decision or fill (i.e. a real availability incident, not a hypothetical).
2. A paper week cannot complete due to host availability (network drop > tolerated window, hardware fault, planned downtime that blocks an RTH session).
3. A second human needs remote monitoring or control of the live runtime.
4. The architecture moves from local research to long-running unattended operation (e.g. the bot is expected to trade for weeks without supervision).
5. Live market-data or network constraints make the local topology unreliable in a way Trusted-IPs and `host_daemon` hardening cannot fix.

The migration is a deliberate project (Gateway+IBC reconfig on the new host, IBKR Trusted IPs update — security-sensitive, network/firewall, container-network proof against error 420). It is not undertaken before a trigger fires.

## Consequences

**Positive:**
- Zero migration cost. Every Phase 1–9 hardening (paper-port guard, `_next_bar_or_shutdown` race, `[BAR]` heartbeat, recovery flatten, three-way reconciler) keeps working unchanged.
- Shadow strategy onboarding is a registry refactor of `host_daemon.py` and a strategy spec, not a topology project.
- NSSM-wrapping is a single small supervisor investment that delivers the "PAUSED desired-state survives crash + reboot" property of the command channel (Resolution 7 of deployment plan § 16).
- The "should we VPS this now?" question stops recurring because the triggers are written.

**Negative:**
- Single point of failure remains the developer laptop. Acceptable as long as the laptop is functionally always-on; if not, trigger #1 or #2 fires and (T4) is paid for.
- Windows-specific platform constraints persist (`add_signal_handler` no-op, `SIGUSR1`/`SIGUSR2` unavailable). Already handled in current code (`_next_bar_or_shutdown` race) and in the command-channel design (file-based polling, not signals).
- Compose does not give the bots uniform `restart: always` supervision; NSSM does, but it's a per-OS wrapper to maintain. Acceptable until (T4) trigger fires.

**Non-consequences:**
- One Gateway with multiple `clientId`s is confirmed as the intra-host pattern. No second Gateway unless an IBKR-side constraint forces it.
- The executable live runtime currently streams IBKR `reqRealTimeBars`; deployable specs log each `DecisionRow.bar_source` as `ibkr_realtime_bars`. Specs that advertise `ibkr_paper_delayed` must be rejected at deploy/start until a delayed-paper runtime adapter exists, because otherwise the artifact provenance hides market-data readiness failures behind a source the engine never actually opened.

## References

- `docs/ibkr-integration-authority.md` § 12 — operator path, host-venv constraint, error 420 history.
- `docs/runbooks/ibkr-paper-dry-run.md` — current host-venv operator runbook.
- `PythonDataService/app/engine/live/host_daemon.py` — supervisor to be extended to N processes.
- `compose.yaml` — observability stack only; no Gateway or bot services.
- `docs/ibkr-paper-deployment-plan.md` § 16 — design-lock resolutions and PR queue.
