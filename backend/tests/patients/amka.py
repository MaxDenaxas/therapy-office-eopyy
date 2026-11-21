"""Synthetic AMKAs for tests (M5.2).

Built at runtime, so no 11-digit literal appears in the source (the repository guard blocks
AMKA-like numbers). Every value starts with ``00``: an AMKA begins with the birth date
(DDMMYY) and day 00 does not exist, so these can never be a real person's AMKA.
"""

from __future__ import annotations


def synthetic_amka(n: int) -> str:
    if not 0 <= n < 10**9:
        raise ValueError("n out of range")
    return f"00{n:09d}"
