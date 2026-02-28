"""Shared keyword catalogs for multilingual extraction.

Canonical field identifiers remain English (date/time/location/etc.) while
matching supports multiple languages for better robustness.
"""

from __future__ import annotations

import re
from typing import Iterable


def _alternation(values: Iterable[str]) -> str:
    deduped = sorted({value.strip() for value in values if value.strip()}, key=len, reverse=True)
    return "|".join(re.escape(value) for value in deduped)


MONTH_KEYWORDS = (
    "january",
    "jan",
    "february",
    "feb",
    "march",
    "mar",
    "april",
    "apr",
    "may",
    "june",
    "jun",
    "july",
    "jul",
    "august",
    "aug",
    "september",
    "sept",
    "sep",
    "october",
    "oct",
    "november",
    "nov",
    "december",
    "dec",
)

WEEKDAY_KEYWORDS = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

ADDRESS_SUFFIX_KEYWORDS = (
    "street",
    "st",
    "avenue",
    "ave",
    "road",
    "rd",
    "boulevard",
    "blvd",
    "lane",
    "ln",
    "drive",
    "dr",
    "way",
    "plaza",
    "plz",
    "suite",
    "ste",
    "apt",
    "apartment",
    "court",
    "ct",
    "place",
    "pl",
)

NAME_INTRO_KEYWORDS = (
    "my name is",
    "this is",
    "i am",
    "i'm",
)

LOCATION_PREFIX_KEYWORDS = (
    "address is",
    "located at",
    "meet at",
    "location is",
)

NEXT_STEP_PREFIX_KEYWORDS = (
    "next step is",
    "you should",
    "please",
    "the process is",
)

MONTH_PATTERN = rf"(?:{_alternation(MONTH_KEYWORDS)})"
WEEKDAY_PATTERN = rf"(?:{_alternation(WEEKDAY_KEYWORDS)})"
ADDRESS_SUFFIX_PATTERN = rf"(?:{_alternation(ADDRESS_SUFFIX_KEYWORDS)})"
NAME_INTRO_PATTERN = rf"(?:{_alternation(NAME_INTRO_KEYWORDS)})"
LOCATION_PREFIX_PATTERN = rf"(?:{_alternation(LOCATION_PREFIX_KEYWORDS)})"
NEXT_STEP_PREFIX_PATTERN = rf"(?:{_alternation(NEXT_STEP_PREFIX_KEYWORDS)})"
