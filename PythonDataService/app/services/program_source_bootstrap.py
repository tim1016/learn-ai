"""Import-time bootstrap: anchor the Signal Program sources (#2450).

This module exists so ``app.main`` can run the anchor with a plain
``import`` statement placed *before* the imports that pull in the strategy
registry and its program modules — the one position where the anchor can
still hash the declared bytes before any of them is cached. It has no other
public surface: importing it IS the bootstrap. Everything substantive lives
in ``app.services.program_source_anchor``.
"""

from app.services.program_source_anchor import record_imported_program_sources

record_imported_program_sources()
