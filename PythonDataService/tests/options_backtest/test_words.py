"""The detectors that prove no sentence of the feature names a date, holds JSON or names a file (#3030).

Every test of the feature's words stands on ``_words.py``. A weakened pattern would pass all of them, so the detectors
are held here: strings they must catch, in the spellings an owner or a lake could write them, and strings they must
leave alone, among them the contract names that encode a date and are identifiers. Hand-worked figures: the instant
1 791 379 860 000 ms is 2026-10-07 13:31 UTC (the 09:31 New York minute of a Wednesday), and divided by a thousand it is
1 791 379 860 seconds, ten digits.
"""

from __future__ import annotations

import pytest

from tests.options_backtest._words import assert_plain, is_plain

DATES = [
    "2026-10-07",
    "the session of 2026-10-07 was refused",
    "10/07/2026",
    "10/7/26",
    "20261007",
    "an instant of 1791379860000",
    "an instant of 1791379860",
    "Oct 7",
    "October 7",
    "7 Oct",
    "Oct. 7, 2026",
    "Tuesday, October 7",
    "march 7",
    "MARCH 7",
    "oct 7",
    "7th October",
    "7th october",
    "October 7th",
    "2026.10.07",
    "07.10.2026",
]
JSON_AND_PATHS = [
    '{"status": "ERROR"}',
    '["a", "b"]',
    'the vendor said "error": "x"',
    "/app/data/lake/raw/spy",
    "(/Users/owner/lake/raw)",
    "lake/raw/spy/20261007_trade.zip",
    "the file spy.zip",
    "REPORT.JSON",
    "C:\\lake\\raw",
]
PLAIN = [
    "SPY261007C00500000",
    "The bars of SPY261007C00500000 could not be fetched (provider_no_data).",
    "1 of 5 sessions",
    "Starting cash is $10,000.00.",
    "The entry is read 5 minutes after the open (09:35 on a full day).",
    "A run takes at most 1300 trading days.",
    "up to 5 may be skipped",
    "The market 5 minutes after the open.",
    "a decrease 5 percent",
    "The wait is 360 seconds.",
    "0.65 per contract and 0.3% slippage",
    "10 contracts, 20 re-entries and 1300 sessions",
    "Waiting for data another request is already fetching.",
    "The other request has finished; carrying on.",
    "the data vendor had no data to give",
    "12345678",
]


@pytest.mark.parametrize("text", [*DATES, *JSON_AND_PATHS])
def test_a_sentence_that_names_a_date_holds_json_or_names_a_file_is_caught(text: str) -> None:
    assert not is_plain(text)
    with pytest.raises(AssertionError):
        assert_plain(text)


@pytest.mark.parametrize("text", PLAIN)
def test_a_sentence_with_no_date_json_or_file_in_it_is_plain_and_a_contract_name_is_an_identifier(text: str) -> None:
    assert is_plain(text)
    assert_plain(text)
