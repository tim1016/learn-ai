"""User-owned broker configuration profiles (ADR 0060, plan package B).

The persistence layer and API surface behind
``/api/brokers/alpaca/configuration``: a dedicated versioned SQLite database on
the Clerk volume holding the local owner, named profiles, their immutable
revisions, account nicknames, the installation selection with its one-shot
Apply request, and an append-only configuration event log.

Deliberately *not* here: credential resolution and broker account verification
(package C — see ``seams.py``), and startup resolution of the effective
selection into a running worker (package D). Nothing in this package changes
broker or worker startup behaviour.

No re-exports. Import from the module that owns the name — ``.service``,
``.store``, ``.runtime``, ``.errors``, ``.records``, ``.envelope``, ``.seams``
— so that importing one of them does not drag the rest of the package, and the
alpaca stack behind ``.envelope``, into an importer's graph.
"""

from __future__ import annotations
