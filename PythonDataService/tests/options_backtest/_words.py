"""What a sentence an owner reads may not hold: a date, a vendor's JSON, a file path (#3030).

A session is named by its open in ms beside the sentence, so no sentence names a date; and what the lake or a vendor
wrote (which can be a date, raw JSON or a path) is replaced by words composed from the facts. These are the detectors
the tests use to hold that. A contract's name (``SPY261007C00500000``) is an identifier and not a date in words.
"""

from __future__ import annotations

import re

_MONTH = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?"
LOOKS_LIKE_A_DATE = re.compile(
    rf"""
    \d{{4}}-\d{{2}}-\d{{2}}                                                 # 2026-10-07
  | \d{{1,2}}/\d{{1,2}}/\d{{2,4}}                                           # 10/7/2026
  | (?<!\d)(?:19|20)\d{{2}}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)   # 20261007
  | (?<!\d)1\d{{12}}(?!\d)                                                  # an instant in ms
  | \b{_MONTH}\s+\d{{1,2}}\b                                                # Oct 7
  | \b\d{{1,2}}\s+{_MONTH}                                                  # 7 October
    """,
    re.VERBOSE,
)
HOLDS_JSON_OR_A_PATH = re.compile(
    r"""[{}\[\]]            # a brace or a bracket
      | "\w+"\s*:           # a JSON key
      | (?:^|[\s(])/[\w.-]+/[\w./-]+   # an absolute path
      | \w+\.(?:zip|json|csv|py)\b     # a file name
      | \\                  # a Windows path or an escape
    """,
    re.VERBOSE,
)


def is_plain(text: str) -> bool:
    return not LOOKS_LIKE_A_DATE.search(text) and not HOLDS_JSON_OR_A_PATH.search(text)


def assert_plain(text: str) -> None:
    date = LOOKS_LIKE_A_DATE.search(text)
    assert date is None, f"a date in words ({date.group(0)!r}): {text}"
    other = HOLDS_JSON_OR_A_PATH.search(text)
    assert other is None, f"raw JSON or a file path ({other.group(0)!r}): {text}"
