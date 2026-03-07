"""Shared keyword catalogs for English canonical extraction."""

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

MONTH_PATTERN = rf"(?:{_alternation(MONTH_KEYWORDS)})"
ADDRESS_SUFFIX_PATTERN = rf"(?:{_alternation(ADDRESS_SUFFIX_KEYWORDS)})"
NAME_INTRO_PATTERN = rf"(?:{_alternation(NAME_INTRO_KEYWORDS)})"
