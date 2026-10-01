---
name: implement
description: "Implement a piece of work based on a spec or set of tickets."
disable-model-invocation: true
---

Implement the work described by the user in the spec or tickets.

Use /tdd where possible, at pre-agreed seams.

Run typechecking and single test files regularly. Before handing off, run lint plus the test that proves the change; CI runs the rest.

Once done, use /code-review to review the work.

Commit your work to the current branch.
