"""app.text: name / notes / AMKA rules and the patient search fold (M5.2, decisions G, L, W, X)."""

from __future__ import annotations

import unicodedata

import pytest

from app.text import (
    TextRuleError,
    mask_amka,
    normalize_amka,
    normalize_name,
    normalize_notes,
    search_tokens,
    therapy_fold,
)
from tests.patients.amka import synthetic_amka

NFD_ALPHA_ACUTE = "α\u0301"  # α + combining acute = ά decomposed


# ---------------------------------------------------------------- names
def test_names_are_nfc_trimmed_and_collapsed():
    assert normalize_name("  Γιώργος   Παπα  ", "first_name") == "Γιώργος Παπα"
    assert normalize_name(NFD_ALPHA_ACUTE + "ννα", "first_name") == "άννα"  # NFC form
    assert normalize_name("ΑΝΝΑ-ΜΑΡΙΑ", "first_name") == "ΑΝΝΑ-ΜΑΡΙΑ"  # case kept as typed


@pytest.mark.parametrize(
    "bad",
    [
        "Γιώργος\tΠαπα",  # tab: never turned into a space
        "Γιώργος\nΠαπα",
        "Γιώργος\r",
        " Γιώργος\t",  # even at the ends: not silently trimmed
        "Γιώρ\x00γος",
        "Γιώρ\x7fγος",
        "Γιώρ\x85γος",  # C1 control (NEL)
        "Γιώρ\u2028γος",  # line separator
        "Γιώρ\u2029γος",
        "Γιώρ\u200bγος",  # zero-width space (Cf)
        "Γιώρ\u202eγος",  # right-to-left override (Cf)
        "Γιώρ\u00adγος",  # soft hyphen (Cf)
        "Γιώργος\u00a0Παπα",  # no-break space (decision W)
        "Γιώργος\u3000Παπα",  # ideographic space
    ],
)
def test_names_refuse_controls_format_characters_and_other_spaces(bad):
    with pytest.raises(TextRuleError) as info:
        normalize_name(bad, "first_name")
    assert info.value.code == "invalid_value"
    assert "not allowed" in info.value.message
    assert "Γιώρ" not in info.value.message  # the value is never in the message


def test_name_length_is_counted_after_normalisation():
    assert len(normalize_name("α" * 100, "last_name")) == 100
    assert normalize_name("   " + "α" * 100 + "   ", "last_name") == "α" * 100  # padding free
    assert normalize_name("α" * 50 + "     " + "β" * 49, "last_name") == "α" * 50 + " " + "β" * 49
    with pytest.raises(TextRuleError, match="at most 100"):
        normalize_name("α" * 101, "last_name")
    # 100 characters once NFC is applied, more before it
    assert len(normalize_name(NFD_ALPHA_ACUTE * 100, "last_name")) == 100


@pytest.mark.parametrize("empty", ["", " ", "     "])
def test_blank_names_are_refused(empty):
    with pytest.raises(TextRuleError, match="must not be empty"):
        normalize_name(empty, "father_name")


def test_huge_and_non_text_names_are_refused_before_any_work():
    with pytest.raises(TextRuleError, match="too long"):
        normalize_name("α" * 5000, "first_name")
    with pytest.raises(TextRuleError, match="must be text"):
        normalize_name(42, "first_name")


# ---------------------------------------------------------------- notes
def test_notes_keep_interior_line_breaks_and_tabs():
    assert normalize_notes("  γραμμή 1\n\tγραμμή 2  \n\n") == "γραμμή 1\n\tγραμμή 2"
    assert normalize_notes("α\u00a0β") == "α\u00a0β"  # other spaces are allowed in notes
    assert normalize_notes(NFD_ALPHA_ACUTE) == "ά"


@pytest.mark.parametrize("blank", [None, "", "   ", "\n\t \n"])
def test_blank_notes_become_none(blank):
    assert normalize_notes(blank) is None


@pytest.mark.parametrize("bad", ["α\rβ", "α\x00", "α\x0bβ", "α\u2028β", "α\u2029β", "α\u200bβ"])
def test_notes_refuse_other_controls_and_separators(bad):
    with pytest.raises(TextRuleError, match="not allowed"):
        normalize_notes(bad)


def test_notes_length_limit():
    assert len(normalize_notes("α" * 2000)) == 2000
    assert len(normalize_notes("\n" + "α" * 2000 + "\n  ")) == 2000
    with pytest.raises(TextRuleError, match="at most 2000"):
        normalize_notes("α" * 2001)
    with pytest.raises(TextRuleError, match="too long"):
        normalize_notes("α" * 9000)


# ---------------------------------------------------------------- AMKA
def test_amka_is_exactly_eleven_ascii_digits():
    value = synthetic_amka(7)
    assert normalize_amka(value) == value
    assert normalize_amka(f"  {value} ") == value  # ordinary spaces at the ends only


@pytest.mark.parametrize(
    "bad",
    [
        synthetic_amka(7)[:-1],  # 10 digits
        synthetic_amka(7) + "0",  # 12 digits
        synthetic_amka(7)[:5] + " " + synthetic_amka(7)[5:],  # inner space: not repaired
        synthetic_amka(7)[:5] + "-" + synthetic_amka(7)[6:],
        "٠" * 11,  # Arabic-Indic digits (\d would accept them)
        "０" * 11,  # full-width digits
        "\t" + synthetic_amka(7),  # only ordinary spaces are trimmed
        synthetic_amka(7)[:-1] + "A",
        "",
        " " * 100,
    ],
)
def test_anything_else_is_refused_without_echoing_it(bad):
    with pytest.raises(TextRuleError) as info:
        normalize_amka(bad)
    assert info.value.message == "amka must be exactly 11 digits (0-9)"


def test_there_is_no_checksum_rule():
    """Decision G: any 11 ASCII digits are accepted (no Luhn or other check digit)."""
    for n in range(10):
        assert normalize_amka(synthetic_amka(n))


def test_the_mask_shows_only_the_last_four_digits():
    value = synthetic_amka(123456789)
    masked = mask_amka(value)
    assert masked == "•" * 7 + value[-4:]
    assert value[:7] not in masked


# ---------------------------------------------------------------- fold
@pytest.mark.parametrize(
    "a, b",
    [
        ("ΆΝΝΑ", "αννα"),
        ("Σταύρος", "ΣΤΑΥΡΟΣ"),  # final ς and capital Σ both fold to σ
        ("σταυρος", "ΣΤΑΥΡΌΣ"),
        ("ΐ", "ι"),
        ("ΰ", "υ"),
        ("Ϊ", "ι"),
        ("ά", NFD_ALPHA_ACUTE),  # precomposed and decomposed
        ("Άννα-Μαρία", "αννα μαρια"),  # hyphen and punctuation become spaces
        ("  Α.  Β  ", "α β"),
        ("µ", "μ"),  # micro sign folds to Greek mu
    ],
)
def test_fold_equalities(a, b):
    assert therapy_fold(a) == therapy_fold(b)


def test_fold_removes_marks_that_casefold_reintroduces():
    assert any(unicodedata.combining(c) for c in "ΐ".casefold())  # why the second NFD exists
    assert therapy_fold("ΐΰ") == "ιυ"


def test_latin_and_greek_letters_stay_distinct():
    """Decision S: no transliteration and no lookalike matching."""
    assert therapy_fold("A") != therapy_fold("Α")
    assert therapy_fold("Papadopoulos") != therapy_fold("Παπαδόπουλος")


def test_fold_of_non_text_is_none_and_tokens_split_on_spaces():
    assert therapy_fold(None) is None and therapy_fold(5) is None
    assert search_tokens("  Παπα   γιώρ-γος ") == ["παπα", "γιωρ", "γοσ"]  # final ς → σ
    assert search_tokens("%_\\") == []


# ---------------------------------------------------------------- the SQLite function
def test_the_fold_is_registered_on_read_and_write_connections(engine):
    from sqlalchemy import text as sql

    from app.db import read_only_engine

    for eng in (engine, read_only_engine(engine)):
        with eng.connect() as conn:
            assert conn.execute(sql("SELECT therapy_fold('ΣΤΑΎΡΟΣ-Ά')")).scalar() == "σταυροσ α"
            assert conn.execute(sql("SELECT therapy_fold(NULL)")).scalar() is None
            assert conn.execute(sql("SELECT therapy_fold(5)")).scalar() is None


def test_no_trigger_check_or_index_uses_the_fold(db_path):
    import sqlite3

    con = sqlite3.connect(db_path)
    try:
        ddl = " ".join(r[0] or "" for r in con.execute("SELECT sql FROM sqlite_master"))
    finally:
        con.close()
    assert "therapy_fold" not in ddl


# ---------------------------------------------------------------- review fixes (M5.2)
LONE_SURROGATES = ["\ud800", "\udfff", "Γιώρ\udc00γος"]


@pytest.mark.parametrize("bad", LONE_SURROGATES)
def test_lone_surrogates_are_refused_in_names_and_notes(bad):
    with pytest.raises(TextRuleError, match="not allowed"):
        normalize_name(bad, "first_name")
    with pytest.raises(TextRuleError, match="not allowed"):
        normalize_notes("σημείωση " + bad)


@pytest.mark.parametrize(
    "name",
    [
        "-",
        ".",
        "1",
        "123-45",
        "'",
        "\u0301",  # a lone combining mark
        "\u0301\u0308",
        "\ue000",  # private use
        "★",  # ★ symbol
        "΄",  # Greek tonos as a spacing symbol
        "- . -",
    ],
)
def test_names_need_at_least_one_letter(name):
    with pytest.raises(TextRuleError) as info:
        normalize_name(name, "last_name")
    assert info.value.message == "last_name must contain at least one letter"


@pytest.mark.parametrize(
    "name",
    [
        "Γεώργιος",
        "ΐ",  # one letter is enough
        "Ιωάννης Β΄",  # Greek numeral style suffix
        "O'Brien",
        "Jean-Luc",
        "María José",
        "Ελένη-Μαρία 2η",  # digits are fine next to letters
        "Łukasz Żółć",
        "Иван Петрович",  # Cyrillic
        "Გიორგი",  # Georgian
        "Արամ",  # Armenian
        "محمد",  # Arabic
        "דוד",  # Hebrew
        "李小龍",  # Han
        "김민준",  # Hangul
        "สมชาย",  # Thai (with combining vowel marks)
        "देवेन्द्र",  # Devanagari (virama and vowel signs are marks)
        "Nguyễn Văn An",  # Vietnamese, stacked diacritics
        "ʻOkalani",  # Hawaiian ʻokina is a modifier letter (Lm)
    ],
)
def test_legitimate_international_names_are_accepted(name):
    assert normalize_name(name, "first_name") == unicodedata.normalize("NFC", name)
