# Alpaca Clerk disposable paper clean slate

During active development, the sanctioned way to restart a disposable Alpaca
paper account is **clean-slate + regenerate**. Do not build an import or
migration path for throwaway paper authority data: it carries stale custody,
catalog, and idempotency history into a run that should instead be generated
again from its current bot configuration.

Run the management command from a one-shot `python-service` container. The
SQLite authority uses WAL, so management tooling must share the same
host-filesystem boundary as the service volume; running it from the host can
produce an unsafe WAL view.

```bash
python scripts/manage_alpaca_sqlite_clerk.py \
  --artifacts-root /var/lib/alpaca-clerk \
  --account-id PA123456 \
  dev-reset \
  --runner-artifacts-root /var/lib/learn-ai-artifacts
```

`dev-reset` is deliberately paper-only: it obtains the configured Alpaca mode
from the service environment and rejects a non-paper configuration. It requires
a cleanly stopped authority, then moves the SQLite or legacy JSONL authority
artifacts and the account's disposable runner catalogs into an account-local
`dev-reset-quarantine/` directory. It writes a manifest and receipt there;
it never contacts the broker, imports legacy data, or deletes authority data.
Repeated runs after a clean reset report that there is nothing left to reset.

This developer shortcut is not the supervised live-account cutover or reset
ceremony. That production workflow remains evidence-gated. The corresponding
ADR-0035 clarification is deferred to issue #1416's acceptance PR because
ADR 0035 is frozen and must not be edited here.
