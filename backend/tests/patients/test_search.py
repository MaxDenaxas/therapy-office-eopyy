"""Patient search and lists (M5.2, decisions E, P, R): word-start matching on the three name
fields, accent / case / final-sigma insensitive, literal wildcards, exact AMKA, deterministic
order and offset/limit pagination."""

from __future__ import annotations

from datetime import datetime

import pytest

from app.services import patients as svc
from tests.patients.amka import synthetic_amka
from tests.patients.conftest import person

NOW = datetime(2026, 3, 2, 10, 0, 0)

PEOPLE = {
    1: ("Γεώργιος", "ΠΑΠΑΔΌΠΟΥΛΟΣ", "Νικόλαος"),
    2: ("Άννα-Μαρία", "Οικονόμου", "Σταύρος"),
    3: ("Ιωάννης", "100%_ΔΟΚΙΜΗ", "Πέτρος"),
    4: ("Δημήτρης", "ΚΑΠΑΠΑΣ", "Ηλίας"),
    5: ("Ελένη", "Αβραμίδου", "Γεώργιος"),
}


@pytest.fixture()
def people(session):
    ids = {}
    for n, (first, last, father) in PEOPLE.items():
        row = svc.create_patient(
            session, person(n, first_name=first, last_name=last, father_name=father), NOW
        ).item
        ids[n] = row.id
    return ids


def found(session, people, query, **kw):
    page = svc.search_patients(session, query=query, **kw)
    by_id = {v: k for k, v in people.items()}
    return [by_id[row.id] for row in page.items]


@pytest.mark.parametrize(
    "query, expected",
    [
        ("παπα", [1]),
        ("ΠΑΠΑ", [1]),
        ("Παπαδόπουλος", [1]),
        ("παπα γεωρ", [1]),  # every token must match (AND)
        ("γεωρ παπα", [1]),  # in any order
        ("γεωργιος", [5, 1]),  # first name of 1, father's name of 5 (ordered by surname)
        ("απα", []),  # middle of a word: no match
        ("σταυρος", [2]),  # final sigma
        ("ΣΤΑΥΡΟΣ", [2]),
        ("Σταύρος", [2]),
        ("αννα", [2]),
        ("μαρια", [2]),  # the hyphen separates words
        ("άννα-μαρία", [2]),
        ("100", [3]),
        ("100%", [3]),  # % is not a wildcard
        ("  γεωργιος    νικ  ", [1]),  # whitespace collapses
        ("καπ", [4]),
        ("παπα ηλιας", []),
    ],
)
def test_word_start_matching(session, people, query, expected):
    assert found(session, people, query) == expected


@pytest.mark.parametrize("query", ["%", "_", "\\", "% _", "--", "   "])
def test_a_query_without_letters_or_digits_is_refused_not_a_match_all(session, people, query):
    with pytest.raises(svc.PatientError) as info:
        svc.search_patients(session, query=query)
    assert info.value.code == "invalid_query"


def test_wildcards_inside_a_word_are_literal(session, people):
    assert found(session, people, "δοκιμ") == [3]  # _ in the stored name is a separator
    assert found(session, people, "1_0") == []


def test_at_most_five_tokens(session, people):
    assert found(session, people, "γ γ γ γ γ") == [5, 1]
    with pytest.raises(svc.PatientError, match="at most 5"):
        svc.search_patients(session, query="α β γ δ ε ζ")


def test_query_and_amka_are_exclusive_and_one_is_required(session, people):
    for kw in ({}, {"query": "παπα", "amka": synthetic_amka(1)}):
        with pytest.raises(svc.PatientError) as info:
            svc.search_patients(session, **kw)
        assert info.value.code == "invalid_query"


def test_exact_amka_search(session, people):
    page = svc.search_patients(session, amka=synthetic_amka(2))
    assert [r.id for r in page.items] == [people[2]] and page.total == 1
    assert svc.search_patients(session, amka=synthetic_amka(99)).items == []
    with pytest.raises(svc.PatientError) as info:
        svc.search_patients(session, amka=synthetic_amka(2)[:-1])
    assert info.value.code == "invalid_value"
    # partial AMKA is never a name match either (decision N)
    assert svc.search_patients(session, query=synthetic_amka(2)[-4:]).items == []


def test_the_active_filter_defaults_to_active_patients(session, people):
    svc.set_patient_active(session, people[1], False, NOW)
    assert found(session, people, "γεωργιος") == [5]  # omitted: active only
    assert found(session, people, "γεωργιος", active=True) == [5]
    assert found(session, people, "γεωργιος", active=False) == [1]  # inactive only
    assert found(session, people, "γεωργιος", active=None) == [5, 1]  # internal callers only
    assert svc.search_patients(session, amka=synthetic_amka(1)).items == []
    assert [
        r.id for r in svc.search_patients(session, amka=synthetic_amka(1), active=False).items
    ] == [people[1]]
    assert people[1] not in [r.id for r in svc.list_patients(session).items]
    assert [r.id for r in svc.list_patients(session, active=False).items] == [people[1]]


def test_lists_are_ordered_by_folded_surname_then_first_name_then_id(session, people):
    extra = svc.create_patient(
        session, person(6, first_name="Άλκηστις", last_name="αβραμίδου", father_name="Χ"), NOW
    ).item
    rows = svc.list_patients(session).items
    assert [r.id for r in rows] == [
        people[3],  # "100 δοκιμη": digits sort before letters
        extra.id,  # αβραμιδου, αλκηστις (accent and case ignored: Ά sorts as α)
        people[5],  # αβραμιδου, ελενη
        people[4],  # καπαπας
        people[2],  # οικονομου
        people[1],  # παπαδοπουλος
    ]


def test_pagination(session, people):
    first = svc.list_patients(session, offset=0, limit=2)
    second = svc.list_patients(session, offset=2, limit=2)
    rest = svc.list_patients(session, offset=4, limit=2)
    assert first.total == second.total == rest.total == 5
    ids = [r.id for page in (first, second, rest) for r in page.items]
    assert sorted(ids) == sorted(people.values()) and len(set(ids)) == 5
    assert [r.id for r in svc.list_patients(session).items] == ids
    assert svc.list_patients(session, offset=5).items == []
    assert svc.list_patients(session, active=False).total == 0
    search = svc.search_patients(session, query="γεωργιος", offset=1, limit=1)
    assert search.total == 2 and [r.id for r in search.items] == [people[1]]


@pytest.mark.parametrize("offset, limit", [(-1, 10), (0, 0), (0, 101), (svc.MAX_OFFSET + 1, 1)])
def test_page_bounds(session, offset, limit):
    with pytest.raises(svc.PatientError) as info:
        svc.list_patients(session, offset=offset, limit=limit)
    assert info.value.code == "invalid_value"


def test_search_is_deterministic_for_identical_names(session):
    ids = [
        svc.create_patient(session, person(n), NOW).item.id for n in (7, 8, 9)
    ]  # same names, different AMKAs
    assert [r.id for r in svc.search_patients(session, query="γεωργιος").items] == ids
