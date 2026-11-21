"""Text rules for personal data (M5.2, ARCHITECTURE.md §5.14). Pure functions, no database.

Names (first, last, father's name), in this order (decision L/W):
  1. Unicode NFC;
  2. reject control characters (Cc — tab, newline, CR, NUL, DEL, C1), format characters
     (Cf — zero-width, bidi overrides, soft hyphen), line/paragraph separators (U+2028/U+2029)
     and every whitespace character other than the ordinary space U+0020 (e.g. NBSP);
     nothing is silently converted into a space;
  3. trim ordinary spaces at both ends;
  4. collapse runs of ordinary spaces to one;
  5. 1–100 characters, counted after normalisation;
  6. at least one letter (Unicode category L*, any script): a name made only of punctuation,
     digits, symbols, combining marks or private-use characters is refused.
Lone surrogates (category Cs) are refused in names and notes: they cannot be stored as UTF-8.

Notes (decision X): NFC; LF and TAB allowed; every other Cc / Cf character and U+2028/U+2029
rejected; surrounding whitespace trimmed (interior line breaks and tabs untouched); blank →
``None``; at most 2,000 characters.

AMKA (decision G): exactly 11 ASCII digits after trimming ordinary spaces at both ends. No
checksum, no repair (inner spaces, dashes, non-ASCII digits are refused).

``therapy_fold`` is the patient search / duplicate key: NFD → casefold → NFD (casefold can
re-introduce combining marks, e.g. for ΐ) → drop every combining mark → everything that is not
a letter or digit becomes a space → collapse whitespace. Accents, case and final sigma fold
away; Latin and Greek letters stay distinct (no transliteration, decision S). It is registered
as the SQLite function ``therapy_fold`` (app/db.py) and must never be used by a trigger, CHECK
or index: the database has to stay usable from tools that do not know it. The M5.1 reference
data keep their own fold (``services/reference.py``); this one is for patients only.

Error messages name the rule and the field, never the submitted value.
"""

from __future__ import annotations

import re
import unicodedata

NAME_MAX_LENGTH = 100
NOTES_MAX_LENGTH = 2000
# a loose bound on the raw input, checked before any work is done
NAME_RAW_LIMIT = 4000
NOTES_RAW_LIMIT = 8000
AMKA_LENGTH = 11
SEARCH_MAX_TOKENS = 5

_AMKA_RE = re.compile(r"[0-9]{11}", re.ASCII)
_SPACES_RE = re.compile(" {2,}")
_NOTES_ALLOWED_CONTROLS = frozenset("\n\t")
AMKA_MASK = "•" * 7  # •••••••


class TextRuleError(ValueError):
    """``code`` is stable; ``message`` names the field and the rule, never the value."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _forbidden_in_name(ch: str) -> bool:
    if ch == " ":
        return False
    category = unicodedata.category(ch)
    # Cs: a lone surrogate (possible through a JSON \ud800 escape) cannot be stored as UTF-8
    return category in ("Cc", "Cf", "Cs", "Zl", "Zp", "Zs") or ch.isspace()


def _forbidden_in_notes(ch: str) -> bool:
    if ch in _NOTES_ALLOWED_CONTROLS:
        return False
    return unicodedata.category(ch) in ("Cc", "Cf", "Cs", "Zl", "Zp")


def _has_letter(text: str) -> bool:
    """At least one character of a Unicode letter category (Lu, Ll, Lt, Lm, Lo), any script."""
    return any(unicodedata.category(ch).startswith("L") for ch in text)


def normalize_name(value: object, field: str) -> str:
    if not isinstance(value, str):
        raise TextRuleError("invalid_value", f"{field} must be text")
    if len(value) > NAME_RAW_LIMIT:
        raise TextRuleError("invalid_value", f"{field} is too long")
    text = unicodedata.normalize("NFC", value)
    if any(_forbidden_in_name(ch) for ch in text):
        raise TextRuleError("invalid_value", f"{field} contains characters that are not allowed")
    text = _SPACES_RE.sub(" ", text.strip(" "))
    if not text:
        raise TextRuleError("invalid_value", f"{field} must not be empty")
    if len(text) > NAME_MAX_LENGTH:
        raise TextRuleError(
            "invalid_value", f"{field} must be at most {NAME_MAX_LENGTH} characters"
        )
    if not _has_letter(text):
        raise TextRuleError("invalid_value", f"{field} must contain at least one letter")
    return text


def normalize_notes(value: object, field: str = "notes") -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise TextRuleError("invalid_value", f"{field} must be text")
    if len(value) > NOTES_RAW_LIMIT:
        raise TextRuleError("invalid_value", f"{field} is too long")
    text = unicodedata.normalize("NFC", value)
    if any(_forbidden_in_notes(ch) for ch in text):
        raise TextRuleError("invalid_value", f"{field} contains characters that are not allowed")
    text = text.strip()
    if not text:
        return None
    if len(text) > NOTES_MAX_LENGTH:
        raise TextRuleError(
            "invalid_value", f"{field} must be at most {NOTES_MAX_LENGTH} characters"
        )
    return text


def normalize_amka(value: object, field: str = "amka") -> str:
    if not isinstance(value, str):
        raise TextRuleError("invalid_value", f"{field} must be text")
    text = value.strip(" ") if len(value) <= 64 else ""  # bound the work; too long fails below
    if not _AMKA_RE.fullmatch(text):
        raise TextRuleError("invalid_value", f"{field} must be exactly 11 digits (0-9)")
    return text


def mask_amka(amka: str) -> str:
    """Lists show only the last four digits; the first six encode the date of birth."""
    return AMKA_MASK + amka[-4:]


def therapy_fold(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = unicodedata.normalize("NFD", unicodedata.normalize("NFD", value).casefold())
    text = "".join(ch for ch in text if not unicodedata.category(ch).startswith("M"))
    text = "".join(ch if ch.isalnum() else " " for ch in text)
    return " ".join(text.split())


def search_tokens(query: str) -> list[str]:
    """The folded words of a name query (may be empty: the caller refuses that)."""
    return (therapy_fold(query) or "").split()


__all__ = [
    "AMKA_LENGTH",
    "NAME_MAX_LENGTH",
    "NOTES_MAX_LENGTH",
    "SEARCH_MAX_TOKENS",
    "TextRuleError",
    "mask_amka",
    "normalize_amka",
    "normalize_name",
    "normalize_notes",
    "search_tokens",
    "therapy_fold",
]
