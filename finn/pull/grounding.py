"""Two cheap checks that keep model output honest.

1. `locate_quote` — a quote attributed to a transcript must actually be in the transcript.
   We look for the quote's word 4-grams in the page text, so small transcription slips still
   match but an invented sentence does not.
2. `ungrounded_numbers` — a figure in synthesized prose must exist in the fact pack the model
   was given. Anything else is dropped before it reaches the reader.

Neither needs a model, so they add no cost.
"""
from __future__ import annotations

import re

_WORD = re.compile(r"[a-z0-9]+")
_NUMBER = re.compile(r"(?<![A-Za-z0-9.])[-−]?\d[\d,]*\.?\d*")
_PERIOD_TOKEN = re.compile(r"\b(?:FY|Q|H|CY)\s?'?\d{1,4}\b|\b(?:19|20)\d{2}\b|\b\d{1,2}(?:st|nd|rd|th)\b", re.IGNORECASE)


def _tokens(text: str) -> list[str]:
    return _WORD.findall((text or "").lower().replace("’", "'"))


def _shingles(tokens: list[str], n: int) -> set[tuple[str, ...]]:
    return {tuple(tokens[i : i + n]) for i in range(len(tokens) - n + 1)}


def quote_recall(quote: str, page_text: str) -> float:
    """Share of the quote's word n-grams that occur in `page_text` (0..1)."""
    q = _tokens(quote)
    if len(q) < 3:
        return 0.0
    n = 4 if len(q) >= 8 else 2
    wanted = _shingles(q, n)
    if not wanted:
        return 0.0
    have = _shingles(_tokens(page_text), n)
    return len(wanted & have) / len(wanted)


def locate_quote(quote: str, pages: list[str], page_hint: int | None = None, threshold: float = 0.65) -> tuple[bool, int | None, float]:
    """(verified, 1-based page, best score). Tries the cited page and its neighbours first, then every page.

    A quote that straddles a page break is checked against adjacent pages joined together.
    """
    if not quote or not pages:
        return False, page_hint, 0.0
    order: list[int] = []
    if page_hint and 1 <= page_hint <= len(pages):
        order = [i for i in (page_hint - 1, page_hint, page_hint - 2) if 0 <= i < len(pages)]
    order += [i for i in range(len(pages)) if i not in order]
    best_page, best = page_hint, 0.0
    for index in order:  # pass 1: the quote sits on one page
        score = quote_recall(quote, pages[index])
        if score > best:
            best_page, best = index + 1, score
        if score >= threshold:
            return True, index + 1, score
    for index in order:  # pass 2: it straddles a page break
        if index + 1 >= len(pages):
            continue
        score = quote_recall(quote, pages[index] + " " + pages[index + 1])
        if score > best:
            best_page, best = index + 1, score
        if score >= threshold:
            return True, index + 1, score
    return False, best_page, best


def _normalise_number(token: str) -> float | None:
    try:
        return float(token.replace(",", "").replace("−", "-"))
    except ValueError:
        return None


def numbers_in(text: str) -> list[float]:
    """Figures in prose, ignoring period tokens (FY27, Q2, 2026) that are labels rather than data."""
    cleaned = _PERIOD_TOKEN.sub(" ", text or "")
    out = []
    for match in _NUMBER.findall(cleaned):
        value = _normalise_number(match)
        if value is not None:
            out.append(abs(value))
    return out


def _matches(value: float, allowed: set[float]) -> bool:
    if value in allowed:
        return True
    for candidate in allowed:
        # the model may round what it was given: 16.2 -> 16, 1199033 -> 1.2 (lakh) is NOT accepted,
        # but 95,625 -> 95,600 is (within 0.6%), as is one-decimal or integer rounding.
        if candidate and abs(candidate - value) / candidate <= 0.006:
            return True
        if round(candidate) == value or round(candidate, 1) == value:
            return True
    return False


def ungrounded_numbers(text: str, allowed: set[float], trivial_below: float = 10) -> list[float]:
    """Figures in `text` that do not appear (allowing for rounding) among `allowed`.

    Small whole numbers are treated as counts ("three quarters", "2 of 5") and skipped.
    """
    missing = []
    for value in numbers_in(text):
        if value < trivial_below and float(value).is_integer():
            continue
        if not _matches(value, allowed):
            missing.append(value)
    return missing


def allowed_numbers(*texts: str) -> set[float]:
    out: set[float] = set()
    for text in texts:
        out.update(numbers_in(text))
    return out
