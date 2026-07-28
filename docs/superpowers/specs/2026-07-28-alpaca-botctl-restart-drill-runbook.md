# Alpaca Bot Control v2 — container-restart drill runbook (S5, #1263)

**Parent:** #1258 (principles P2/P3/P4, decision L1). A data-plane restart
kills every in-container bot task by construction. This drill proves that is
a **recovery, not an incident** — run it after any change to the bot runner,
the clerk recovery path, or the container lifecycle.

## Automated portions

`PythonDataService/tests/services/test_boot_recovery.py` runs the drill in
integration shape on every CI pass:

- `test_named_restart_drill_end_to_end` — kill with a bot on duty, an open
  attributed paper position, and a response-lost submit in flight → restart →
  interrupted evidence, no duplicate orders, exposure intact and attributed,
  unresolved intents terminally classified, honest operator surface, clean
  re-start with journal-owned hydration.
- `test_unresolved_intent_resolves_by_identity_without_duplicates` — the
  three resolution branches (present → adopted, provably absent →
  not-accepted, otherwise → held uncertain), each with a
  no-duplicate-submission assertion.
- `test_starts_refused_*` — the fail-closed start gate.

## Manual drill (live paper account)

Preconditions: Alpaca paper keys configured; IBKR gateway up (shared feed
healthy); one log-only bot deployable.

1. **Arm.** Deploy a bot via `POST /api/brokers/alpaca/bots`
   (`strategy_instance_id`, `symbol`). Confirm `running=true`, `ON_DUTY` in
   the roster, and a live bar decision in the data-plane logs
   (`action=bot_decision`).
2. **Open exposure.** Submit a one-share paper order through the clerk with
   the bot's namespace and let it fill (or use an existing S3 test harness
   order). Confirm the per-instance projection shows the position.
3. **Kill.** `podman kill polygon-data-service` (a hard stop — not
   `podman stop`, which would run the graceful shutdown path).
4. **Restart.** `podman start polygon-data-service`. Watch the boot logs for
   `action=boot_sweep_interrupted` (the bot) and
   `action=boot_recovery_complete` (`unresolved_intents` count).
5. **Verify honesty.** `GET /api/brokers/alpaca/bots/{sid}` →
   `running=false`, `phase=OFF_DUTY`,
   `duty_outcome.kind=EXITED_UNVERIFIED`,
   `duty_outcome.reason_code=INTERRUPTED_BY_RESTART`. The bot must NOT have
   auto-restarted.
6. **Verify money safety.** At Alpaca (paper dashboard or
   `GET /api/brokers/alpaca/orders`): no duplicate orders; the position is
   unchanged. The clerk journal (`order_journal.jsonl`) shows every pre-kill
   intent terminally classified (`submit_acked`/`submit_failed`) or — if the
   broker was unreachable during boot — still `submit_uncertain`, in which
   case **bot starts stay refused** until a later replay resolves it.
7. **Resume.** Re-deploy the bot. It must hydrate exactly its journal-owned
   exposure (the per-instance projection — never the account-net map) and
   resume consuming bars.

## Abort criteria

Stop the drill and open an incident (do not re-drill) if any of: a duplicate
order appears at the broker; a bot renders `running=true`/`ON_DUTY` without
its task; exposure attribution changes across the restart; or a start is
accepted while `unresolved_intents > 0`.
