"""F5-T4 — tests for the offline address normalizer (normalize.py).

Pure stdlib functions, no network, no fixtures: this file runs on its own.
Covers suffix/directional expansion, idempotence, punctuation/whitespace
handling, and the invariant that already-abbreviated input is unchanged.
"""
from app.geocoding.normalize import (
    DIRECTIONALS,
    STREET_SUFFIXES,
    normalize_number,
    normalize_street,
)


# --- suffix expansion -------------------------------------------------------

def test_common_suffixes_expand_to_usps_standard():
    assert normalize_street("100 Main Street") == "100 MAIN ST"
    assert normalize_street("100 Main Avenue") == "100 MAIN AVE"
    assert normalize_street("100 Main Boulevard") == "100 MAIN BLVD"
    assert normalize_street("100 Main Road") == "100 MAIN RD"
    assert normalize_street("100 Main Drive") == "100 MAIN DR"
    assert normalize_street("100 Main Court") == "100 MAIN CT"
    assert normalize_street("100 Main Lane") == "100 MAIN LN"
    assert normalize_street("100 Main Place") == "100 MAIN PL"
    assert normalize_street("100 Main Parkway") == "100 MAIN PKWY"
    assert normalize_street("100 Main Terrace") == "100 MAIN TER"


# --- directional standardization --------------------------------------------

def test_directionals_expand_to_single_letter():
    assert normalize_street("121 North La Salle Street") == "121 N LA SALLE ST"
    assert normalize_street("50 South Michigan Ave") == "50 S MICHIGAN AVE"
    assert normalize_street("1 East Wacker Dr") == "1 E WACKER DR"
    assert normalize_street("1 West Randolph St") == "1 W RANDOLPH ST"


def test_two_letter_directionals():
    assert normalize_street("10 Northeast Broadway") == "10 NE BROADWAY"
    assert normalize_street("10 Southwest Trail") == "10 SW TRL"


# --- already-abbreviated input is unchanged ---------------------------------

def test_already_abbreviated_street_is_unchanged():
    assert normalize_street("121 N LA SALLE ST") == "121 N LA SALLE ST"
    assert normalize_street("50 S MICHIGAN AVE") == "50 S MICHIGAN AVE"


def test_already_normalized_number_is_unchanged():
    assert normalize_number("121") == "121"
    assert normalize_number("121-A") == "121-A"


# --- punctuation and whitespace ---------------------------------------------

def test_commas_and_periods_collapse_to_spaces():
    assert normalize_street("121 N. La Salle St., Chicago") == "121 N LA SALLE ST CHICAGO"


def test_extra_whitespace_collapses():
    assert normalize_street("  121   North    La Salle   Street  ") == "121 N LA SALLE ST"


def test_lowercase_input_uppercased():
    assert normalize_street("121 north lasalle st") == "121 N LASALLE ST"


# --- house number normalization ---------------------------------------------

def test_number_strips_and_uppercases():
    assert normalize_number("  121 ") == "121"
    assert normalize_number("121-a") == "121-A"


def test_number_rejoins_padded_hyphen():
    assert normalize_number("121 - A") == "121-A"
    assert normalize_number("121-A") == normalize_number("121 - A")


def test_number_preserves_leading_zeros():
    assert normalize_number("007") == "007"


# --- idempotence ------------------------------------------------------------

def test_normalize_street_is_idempotent():
    samples = [
        "121 North La Salle Street",
        "121 N LA SALLE ST",
        "  50 South Michigan Avenue, Chicago  ",
        "10 Northeast Broadway Parkway",
        "plain proper name only",
    ]
    for sample in samples:
        once = normalize_street(sample)
        assert normalize_street(once) == once


def test_normalize_number_is_idempotent():
    for sample in ["121", "121-A", "  121 - a ", "007"]:
        once = normalize_number(sample)
        assert normalize_number(once) == once


# --- table invariants -------------------------------------------------------

def test_tables_are_self_stable():
    # Every standard form must map to itself, or normalization could not be
    # idempotent and abbreviated input would drift.
    for standard in set(DIRECTIONALS.values()):
        assert DIRECTIONALS[standard] == standard
    for standard in set(STREET_SUFFIXES.values()):
        assert STREET_SUFFIXES[standard] == standard


def test_unknown_tokens_pass_through():
    # A street proper name that is neither directional nor suffix is untouched.
    assert normalize_street("Wabash") == "WABASH"


def test_empty_input():
    assert normalize_street("") == ""
    assert normalize_number("") == ""
