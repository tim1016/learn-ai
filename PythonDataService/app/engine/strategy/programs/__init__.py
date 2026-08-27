"""Per-program parameter models and Signal Program factories (issue #1735).

One module per Signal Program, each holding that program's parameter model,
its sealed identity constants, and the factory that wires the parameters to
the math. Splitting them out of ``registry.py`` is what lets a program's
``artifact_paths`` name its own wiring: the registry imports eleven
algorithm modules, so listing it would make every program's closure depend
on every other program's math.

Each module declares its program's sealed identity once, referenced by both
its construction seam (where it becomes the evaluation_id hash input) and
its registry contract (where it becomes the build-proof admission identity).
Declared once rather than restated as a literal in each place: a factory
whose program_version drifts from its contract's would produce a bot whose
evaluation traces claim a different program than its seal, and neither hash
would look wrong on its own. ``test_registry_signal_program_identity.py``
enforces the relationship for every registered program, so a new program
that restates a literal instead of following this pattern fails loudly.
"""
