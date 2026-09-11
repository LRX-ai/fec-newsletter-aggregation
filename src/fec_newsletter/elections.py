"""The 2026 federal primary calendar, and what it lets us say about a contribution.

An election designation (FEC `TRANSACTION_PGI`) says which election money was given
for. On its own that separates a general-election contribution -- 3 November 2026,
unambiguously ahead of us -- from a primary one. It does NOT separate primaries that
have happened from primaries that have not, and in 2026 that distinction spans six
months: Texas voted on 3 March, Delaware votes on 15 September, and Louisiana's House
primary was postponed to 3 November, which is after the general everywhere else.

Without the calendar, a "nomination contests" bucket silently mixes settled races with
races still to run, and any claim about how much of a quarter is forward-looking is
understated by however much of that bucket is still ahead.

Source: FEC, *2026 Congressional Primary Dates and Candidate Filing Deadlines for
Ballot Access*, https://www.fec.gov/documents/5910/2026pdates.pdf (data as of
2026-05-18). The FEC's own note applies: dates are set by states, not by the FEC, and
are subject to change -- so re-pull the PDF each quarter rather than trusting this file
indefinitely.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd

from . import config

CALENDAR = config.REPO_ROOT / "fec_primary_calendar_2026.csv"
GENERAL_2026 = dt.date(2026, 11, 3)


def load() -> pd.DataFrame:
    cal = pd.read_csv(CALENDAR, dtype=str).fillna("")
    for col in ("primary_date", "runoff_date"):
        cal[col] = pd.to_datetime(cal[col], errors="coerce").dt.date
    return cal


def _matches(row, office: str, district: str) -> bool:
    """A calendar row applies unless it is scoped to an office or a district list.

    Two states need the scoping. Louisiana runs its Senate primary in May and its House
    primary in November; Alabama splits its districts across May and August. Everywhere
    else one row covers the state.

    Known gap: Alabama's rows are district-scoped, so an Alabama SENATE row (district
    "00") matches neither and the caller gets None, which classifies as undated. The
    FEC table marks Alabama as holding a Senate election but gives no statewide date
    separate from the two district dates, and picking one would be a guess. Undated is
    the honest answer until the FEC publishes it.
    """
    if row["office"] and row["office"] != office:
        return False
    if row["districts"]:
        wanted = {d.strip().lstrip("0") for d in row["districts"].split(",")}
        return str(district).strip().lstrip("0") in wanted
    return True


def primary_date(cal: pd.DataFrame, state: str, office: str = "", district: str = "") -> dt.date | None:
    rows = cal[cal["state"] == str(state).upper()]
    for _, row in rows.iterrows():
        if _matches(row, office, district) and row["primary_date"] is not pd.NaT:
            return row["primary_date"]
    return None


def status(pgi: str, state: str, office: str = "", district: str = "",
           as_of: dt.date | None = None, cal: pd.DataFrame | None = None) -> str:
    """One of: settled, ahead, undated, other-cycle.

    `as_of` is the date the edition is READ, not the end of the quarter it covers. A Q2
    edition published in late August is describing May primaries that are long decided;
    dating the judgement to 30 June would call them live.
    """
    cal = load() if cal is None else cal
    as_of = as_of or dt.date.today()
    p = str(pgi or "").strip().upper()
    if not p:
        return "undated"
    letter, year = p[:1], p[1:]
    if not year.isdigit():
        return "undated"
    year = int(year)
    if year > 2026:
        return "ahead"
    if year < 2026:
        return "other-cycle"
    if letter == "G":
        return "settled" if as_of > GENERAL_2026 else "ahead"
    d = primary_date(cal, state, office, district)
    if d is None:
        return "undated"
    if letter == "R":                       # a runoff trails its primary
        rows = cal[cal["state"] == str(state).upper()]
        for _, row in rows.iterrows():
            if _matches(row, office, district) and row["runoff_date"] is not pd.NaT and row["runoff_date"]:
                d = row["runoff_date"]
                break
    return "settled" if as_of > d else "ahead"
