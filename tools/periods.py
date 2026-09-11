"""Period parsing shared by every tool: a quarter or a month, one accessor.

Its own module because both the edition renderer and the description generator need it,
and each already imports from the other -- putting it in either created a cycle.
"""
from __future__ import annotations
import re

import pandas as pd

PERIOD_RE = re.compile(r"^(\d{4})(?:Q([1-4])|-(\d{2}))$")


def period_mask(df, period, col="transaction_dt"):
    """Row mask for a period given as either "2026Q2" or "2026-06"."""
    m = PERIOD_RE.match(str(period))
    if not m:
        raise ValueError(f"period must look like 2026Q2 or 2026-06, got {period!r}")
    d = pd.to_datetime(df[col])
    year = int(m.group(1))
    if m.group(2):
        q = int(m.group(2))
        lo = pd.Timestamp(year, 1 + 3 * (q - 1), 1)
        hi = lo + pd.offsets.QuarterEnd()
    else:
        lo = pd.Timestamp(year, int(m.group(3)), 1)
        hi = lo + pd.offsets.MonthEnd()
    return (d >= lo) & (d <= hi)


def is_month(period) -> bool:
    return "-" in str(period)


def period_label(period) -> str:
    m = PERIOD_RE.match(str(period))
    if m.group(2):
        return f"{m.group(1)} Q{m.group(2)}"
    return pd.Timestamp(int(m.group(1)), int(m.group(3)), 1).strftime("%B %Y")
