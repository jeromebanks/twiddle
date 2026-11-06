"""Small text helpers for times."""
from __future__ import annotations


def pad2(n: int) -> str:
    """`n` as at least two digits."""
    return f"{n:02d}"
