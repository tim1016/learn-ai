"""What a sentence an owner reads may not hold: a date, a vendor's JSON, a file path (#3030).

A session is named by its open in ms beside the sentence, so no sentence names a date; and what the lake or a vendor
wrote (which can be a date, raw JSON or a path) is replaced by words composed from the facts. These are the detectors
the tests use to hold that, and ``test_words.py`` holds the detectors. A contract's name (``SPY261007C00500000``) is an
identifier and not a date in words.
"""

from __future__ import annotations

import re

# A month by its whole name or its usual short form, with or without the full stop after it.
_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|June?|July?|Aug(?:ust)?|Sep(?:t(?:ember)?)?"
    r"|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?)\.?"
)
_ORDINAL = r"(?:st|nd|rd|th)?"
LOOKS_LIKE_A_DATE = re.compile(
    rf"""
    \d{{4}}-\d{{2}}-\d{{2}}                                                 # 2026-10-07
  | \d{{1,2}}/\d{{1,2}}/\d{{2,4}}                                           # 10/7/2026
  | \d{{4}}\.\d{{1,2}}\.\d{{1,2}}(?!\d)                                     # 2026.10.07
  | (?<!\d)\d{{1,2}}\.\d{{1,2}}\.\d{{2,4}}(?!\d)                            # 07.10.2026
  | (?<!\d)(?:19|20)\d{{2}}(?:0[1-9]|1[0-2])(?:0[1-9]|[12]\d|3[01])(?!\d)   # 20261007
  | (?<!\d)1\d{{12}}(?!\d)                                                  # an instant in ms
  | (?<!\d)1\d{{9}}(?!\d)                                                   # an instant in seconds
  | \b{_MONTH}\s+\d{{1,2}}{_ORDINAL}\b                                      # Oct 7, march 7, October 7th
  | \b\d{{1,2}}{_ORDINAL}\s+(?-i:(?!may\b)){_MONTH}(?![a-z])                # 7 Oct, 7th October (not "5 may be")
    """,
    re.VERBOSE | re.IGNORECASE,
)
HOLDS_JSON_OR_A_PATH = re.compile(
    r"""[{}\[\]]            # a brace or a bracket
      | "\w+"\s*:           # a JSON key
      | (?:^|[\s(])/[\w.-]+/[\w./-]+   # an absolute path
      | \w+\.(?:zip|json|csv|py)\b     # a file name
      | \\                  # a Windows path or an escape
    """,
    re.VERBOSE | re.IGNORECASE,
)


def is_plain(text: str) -> bool:
    return not LOOKS_LIKE_A_DATE.search(text) and not HOLDS_JSON_OR_A_PATH.search(text)


def assert_plain(text: str) -> None:
    date = LOOKS_LIKE_A_DATE.search(text)
    assert date is None, f"a date in words ({date.group(0)!r}): {text}"
    other = HOLDS_JSON_OR_A_PATH.search(text)
    assert other is None, f"raw JSON or a file path ({other.group(0)!r}): {text}"
