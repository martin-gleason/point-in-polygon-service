"""F5-T3 — address normalization for exact offline matching (PLAN D8).

The offline geocoder (local_points.py) matches a queried address against a
reference table by *string equality*. That only works if both sides are written
the same way: "121 North La Salle Street" and "121 N LASALLE ST" are the same
place but not the same string. These pure functions canonicalize both the query
and the reference rows to one form — uppercase, no stray punctuation, single
spaces, USPS-standard directionals and street-type suffixes — so the equality
check is meaningful.

The suffix and directional tables are an *embedded* subset of USPS Publication
28 (Appendix C), not an exhaustive copy: a couple dozen of the common types,
enough for the Chicago / Cook County reference set. Keeping them in-module keeps
the offline path dependency-free (D8) — no `usaddress`, no `scourgify`, nothing
to install or reach for over the network.

ArcGIS / ArcPy equivalent
    This reproduces, in stdlib, the address-standardization stage an ArcGIS
    address locator performs internally from its `.lot.xml` / USPS style file
    before candidate matching — the same normalization ``arcpy.geocoding`` and
    the ArcGIS Python API's `geocode()` apply under the hood when they fold
    "STREET" to "ST" and "NORTH" to "N". Here it is explicit and inspectable
    rather than buried in a locator style.
"""
from __future__ import annotations

import re

# Compass directionals → USPS single-letter form. Includes the already-abbreviated
# forms mapped to themselves so normalization is idempotent (D8).
DIRECTIONALS: dict[str, str] = {
    "NORTH": "N",
    "SOUTH": "S",
    "EAST": "E",
    "WEST": "W",
    "NORTHEAST": "NE",
    "NORTHWEST": "NW",
    "SOUTHEAST": "SE",
    "SOUTHWEST": "SW",
    "N": "N",
    "S": "S",
    "E": "E",
    "W": "W",
    "NE": "NE",
    "NW": "NW",
    "SE": "SE",
    "SW": "SW",
}

# Street-type suffixes → USPS Publication 28 standard abbreviation. A common
# subset, not the full appendix. Each standard form maps to itself so applying
# normalization twice is a no-op (idempotence).
STREET_SUFFIXES: dict[str, str] = {
    "STREET": "ST",
    "ST": "ST",
    "AVENUE": "AVE",
    "AVE": "AVE",
    "BOULEVARD": "BLVD",
    "BLVD": "BLVD",
    "ROAD": "RD",
    "RD": "RD",
    "DRIVE": "DR",
    "DR": "DR",
    "COURT": "CT",
    "CT": "CT",
    "LANE": "LN",
    "LN": "LN",
    "PLACE": "PL",
    "PL": "PL",
    "PARKWAY": "PKWY",
    "PKWY": "PKWY",
    "TERRACE": "TER",
    "TER": "TER",
    "CIRCLE": "CIR",
    "CIR": "CIR",
    "TRAIL": "TRL",
    "TRL": "TRL",
    "HIGHWAY": "HWY",
    "HWY": "HWY",
    "SQUARE": "SQ",
    "SQ": "SQ",
    "PLAZA": "PLZ",
    "PLZ": "PLZ",
    "WAY": "WAY",
    "LOOP": "LOOP",
    "ROW": "ROW",
    "ALLEY": "ALY",
    "ALY": "ALY",
    "CROSSING": "XING",
    "XING": "XING",
    "EXPRESSWAY": "EXPY",
    "EXPY": "EXPY",
    "PIKE": "PIKE",
}

# Any run of characters that is not a letter, digit, or hyphen becomes a space.
# Hyphens survive so "121-A" and "SODO-WELLS" keep their internal structure.
_SEPARATORS = re.compile(r"[^A-Z0-9-]+")


def _clean(value: str) -> str:
    """Uppercase, replace punctuation/whitespace runs with single spaces, and
    trim. Commas and periods (the usual address separators) collapse to spaces;
    hyphens are preserved."""
    upper = value.upper()
    spaced = _SEPARATORS.sub(" ", upper)
    return spaced.strip()


def normalize_street(value: str) -> str:
    """Canonicalize a street line for exact offline matching.

    Uppercases, drops commas/periods and collapses whitespace, then rewrites
    every token that is a known directional or street-type suffix to its USPS
    standard form. Unknown tokens (the street's proper name, the house number)
    pass through untouched.

    Idempotent: ``normalize_street(normalize_street(x)) == normalize_street(x)``,
    because every standard form maps to itself in the tables.

    >>> normalize_street("121 North La Salle Street")
    '121 N LA SALLE ST'
    >>> normalize_street("121 N LASALLE ST")
    '121 N LASALLE ST'
    """
    tokens = _clean(value).split()
    out: list[str] = []
    for token in tokens:
        if token in DIRECTIONALS:
            out.append(DIRECTIONALS[token])
        elif token in STREET_SUFFIXES:
            out.append(STREET_SUFFIXES[token])
        else:
            out.append(token)
    return " ".join(out)


def normalize_number(value: str) -> str:
    """Canonicalize a house number for exact offline matching.

    Uppercases (for a unit letter like "121-A"), strips surrounding whitespace,
    and collapses any internal whitespace/punctuation around a hyphen so
    "121 - A" and "121-a" both become "121-A". Purely lexical — no numeric
    parsing, so leading zeros and ranges survive as written.

    Idempotent.

    >>> normalize_number("  121 ")
    '121'
    >>> normalize_number("121-a")
    '121-A'
    """
    cleaned = _clean(value)
    # A hyphen padded by the space-substitution ("121 - A") rejoins into "121-A".
    return re.sub(r"\s*-\s*", "-", cleaned)
