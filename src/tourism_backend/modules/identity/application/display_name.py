"""Display-name policy: length bounds + lightweight profanity gate."""

from __future__ import annotations

import re
import unicodedata

DISPLAY_NAME_MIN_LENGTH = 1
DISPLAY_NAME_MAX_LENGTH = 20

# Collapsed / token denylist (RU + EN). Keep entries distinctive enough to
# avoid common false positives in short travel nicknames.
_BLOCKED_TERMS: frozenset[str] = frozenset(
    {
        # English
        "fuck",
        "fucker",
        "fucking",
        "shit",
        "bullshit",
        "bitch",
        "asshole",
        "bastard",
        "cunt",
        "dick",
        "cock",
        "pussy",
        "whore",
        "slut",
        "faggot",
        "nigger",
        "nigga",
        "motherfucker",
        # Russian (normalized ё→е)
        "блять",
        "блядь",
        "бляд",
        "сука",
        "суки",
        "хуй",
        "хуя",
        "хуе",
        "хуи",
        "пизда",
        "пиздец",
        "ебать",
        "ебал",
        "ебан",
        "ебаный",
        "пидор",
        "пидар",
        "педик",
        "мудак",
        "мудила",
        "гандон",
        "залупа",
        "еблан",
        "мразь",
        "дебил",
        "уебок",
    }
)

_SHORT_WHOLE_ONLY: frozenset[str] = frozenset(
    {
        "ass",
        "sex",
        "fag",
        "cum",
    }
)

# Short stems matched as substrings (too short for the generic >=4 rule).
_ROOT_SUBSTRINGS: frozenset[str] = frozenset(
    {
        "хуй",
        "хуе",
        "хуя",
        "хуи",
        "бляд",
        "пизд",
        "ебал",
        "ебан",
        "fuck",
        "shit",
        "dick",
        "cunt",
    }
)

_LEET = str.maketrans(
    {
        "0": "o",
        "1": "i",
        "3": "e",
        "4": "a",
        "5": "s",
        "7": "t",
        "@": "a",
        "$": "s",
        "!": "i",
    }
)

_NON_LETTER_RE = re.compile(r"[^a-zа-яё]+", re.IGNORECASE)


def _normalize_for_scan(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value).casefold()
    folded = folded.replace("ё", "е").translate(_LEET)
    return _NON_LETTER_RE.sub("", folded)


def _tokens(value: str) -> list[str]:
    folded = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    folded = folded.translate(_LEET)
    return [token for token in _NON_LETTER_RE.split(folded) if token]


def contains_prohibited_language(value: str) -> bool:
    collapsed = _normalize_for_scan(value)
    if not collapsed:
        return False
    for root in _ROOT_SUBSTRINGS:
        if root in collapsed:
            return True
    for term in _BLOCKED_TERMS:
        if len(term) >= 4 and term in collapsed:
            return True
        if collapsed == term:
            return True
    tokens = _tokens(value)
    for token in tokens:
        if token in _BLOCKED_TERMS or token in _SHORT_WHOLE_ONLY:
            return True
        for term in _BLOCKED_TERMS:
            if len(term) >= 4 and term in token:
                return True
    return False


def validate_display_name(value: str) -> str:
    cleaned = value.strip()
    if len(cleaned) < DISPLAY_NAME_MIN_LENGTH or len(cleaned) > DISPLAY_NAME_MAX_LENGTH:
        raise ValueError(
            f"display_name must be {DISPLAY_NAME_MIN_LENGTH}..{DISPLAY_NAME_MAX_LENGTH} characters"
        )
    if contains_prohibited_language(cleaned):
        raise ValueError("display_name contains prohibited language")
    return cleaned
