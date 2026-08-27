"""FEC candidate -> congressional committee -> newsletter.

This is what the receive pipeline attributes from. A candidate has no catcode, so the
industry signal comes from the committees they sit on: money reaching a member of House
Agriculture is money reaching agricultural policy.

The join is three hops, and each one loses rows:

    FEC CAND_ID  ->  bioguide          via c_github_legislators_current_person.fec
    bioguide     ->  subcommittee code via c_github_committee_membership_current
    subcommittee ->  newsletter(s)     via congress_cmte_crosswalk.csv

Attribution is at **subcommittee** level: "Cybersecurity and Infrastructure Protection"
is a far sharper industry signal than "House Homeland Security". The subcommittee code is
the parent's thomas_id plus the zero-padded subcommittee id (HSAG + "15" -> HSAG15), which
matches all 179 live codes in the membership table exactly.

Only **sitting** members are in the source, so challengers, open-seat candidates and
defeated incumbents resolve to nothing at all — 70% of candidate dollars. That is inherent
to attributing by committee seat, not a defect, but it must never be silent: the runner
reports the excluded share on every run.
"""

from __future__ import annotations

import ast
import re

import pandas as pd

from . import config

# The source file separates newsletters with '/' and occasionally ',', and uses its own
# shorthand rather than the crosswalk's names.
LABEL_SEP = re.compile(r"[/,]")

# Author shorthand -> crosswalk newsletter(s). Keys are lowercased and stripped of spaces
# and '&' so that "ICT", "ICT&Cybersecurity" and "ict & cybersecurity" all collapse to one
# entry. A label may expand to SEVERAL newsletters — "Energy" covers both fuels and grid,
# and the source does not distinguish them.
LABEL_ALIASES: dict[str, tuple[str, ...]] = {
    "agri": ("Agri-Food",),
    "agriculture": ("Agri-Food",),
    "foodbeverage": ("Agri-Food",),
    "automotive": ("Automotive",),
    "aviation": ("Freight & Transport",),
    "trade": ("Freight & Transport",),
    "freight": ("Freight & Transport",),
    "freightports": ("Freight & Transport",),
    "biopharma": ("Healthcare",),
    "healthcare": ("Healthcare",),
    "construction": ("Construction & Housing",),
    "constructionhousing": ("Construction & Housing",),
    "defence": ("Defense",),
    "defense": ("Defense",),
    "energy": ("Oil & Gas", "Utilities & Power"),
    "utilities": ("Utilities & Power",),
    "utilitiespower": ("Utilities & Power",),
    "oilgas": ("Oil & Gas",),
    "environment": ("Environment",),
    "finance": ("Finance & Insurance",),
    "financeinsurance": ("Finance & Insurance",),
    "ict": ("ICT & Cybersecurity",),
    "ictcybersecurity": ("ICT & Cybersecurity",),
    "indigenous": ("Tribal Affairs",),
    "tribalaffairs": ("Tribal Affairs",),
    "manufacturing": ("Manufacturing",),
    "mining": ("Mining",),
    "retail": ("Hospitality & Retail",),
    "hospitalityretail": ("Hospitality & Retail",),
    "social": ("Social Issues",),
    "socialissues": ("Social Issues",),
    "highered": ("Higher Ed",),
    "fisheries": ("Fisheries",),
    "foreignaffairs": ("Foreign Affairs",),
}

# Every regional/diplomatic subcommittee of these two committees was labelled only
# "Social Issues" in the source, leaving the Foreign Affairs newsletter with no money at
# all. Confirmed an oversight, so Foreign Affairs is added alongside the existing labels
# rather than replacing them.
FOREIGN_AFFAIRS_COMMITTEES = frozenset({"HSFA", "SSFR"})

MAPPING_COLUMNS = [
    "name", "url", "thomas_id", "youtube_id", "jurisdiction_source",
    "subcommittee_name", "subcommittee_thomas_id", "newsletters", "notes",
]


def _normalise_label(label: str) -> tuple[str, ...]:
    key = re.sub(r"[^a-z0-9]", "", label.lower())
    return LABEL_ALIASES.get(key, ())


def parse_newsletters(raw: str, committee: str = "") -> list[str]:
    """Author shorthand -> crosswalk newsletter names, deduped and order-stable."""
    out: list[str] = []
    for part in LABEL_SEP.split(raw or ""):
        part = part.strip()
        if not part:
            continue
        for nl in _normalise_label(part):
            if nl not in out:
                out.append(nl)
    if committee in FOREIGN_AFFAIRS_COMMITTEES and "Foreign Affairs" not in out:
        out.append("Foreign Affairs")
    return out


def unknown_labels(mapping: pd.DataFrame | None = None) -> set[str]:
    """Labels in the source file that no alias covers — these silently attribute nothing."""
    mapping = load_mapping() if mapping is None else mapping
    seen = set()
    for raw in mapping["newsletters"]:
        for part in LABEL_SEP.split(raw or ""):
            part = part.strip()
            if part and not _normalise_label(part):
                seen.add(part)
    return seen


def load_mapping() -> pd.DataFrame:
    """The authored subcommittee -> newsletter crosswalk.

    The file's header names only 7 of its 9 columns (the newsletter and notes columns are
    unnamed), so the header row is skipped and the columns named positionally.
    """
    path = config.COMMITTEE_NEWSLETTER_MAP
    if not path.exists():
        raise FileNotFoundError(f"committee->newsletter mapping not found at {path}")
    df = pd.read_csv(path, dtype=str, keep_default_na=False, header=None, skiprows=1)
    if df.shape[1] < len(MAPPING_COLUMNS):
        for i in range(df.shape[1], len(MAPPING_COLUMNS)):
            df[i] = ""
    df = df.iloc[:, : len(MAPPING_COLUMNS)]
    df.columns = MAPPING_COLUMNS
    return df


def committee_newsletters(mapping: pd.DataFrame | None = None) -> pd.DataFrame:
    """(code, level, newsletter) for every mapped subcommittee, plus a derived top-level row.

    The top-level rows are the fallback for a member who sits on a committee but holds no
    mapped subcommittee seat there (17 members today): they receive the UNION of that
    committee's subcommittee newsletters. Derived from the same file rather than
    hand-written, so there is one authorship and one source of truth. Committees with no
    subcommittee rows at all (Indian Affairs, Veterans' Affairs, Senate Intelligence,
    Small Business, the joint committees) produce no rows and attribute nothing.
    """
    mapping = load_mapping() if mapping is None else mapping
    sub = mapping[mapping["subcommittee_thomas_id"].str.strip() != ""].copy()

    rows = []
    for _, r in sub.iterrows():
        code = r["thomas_id"].strip() + r["subcommittee_thomas_id"].strip().zfill(2)
        for nl in parse_newsletters(r["newsletters"], r["thomas_id"].strip()):
            rows.append({"code": code, "committee": r["thomas_id"].strip(),
                         "level": "subcommittee", "newsletter": nl})
    detail = pd.DataFrame(rows)
    if detail.empty:
        return detail

    top = (
        detail[["committee", "newsletter"]].drop_duplicates()
        .rename(columns={"committee": "code"})
        .assign(committee=lambda d: d["code"], level="committee")
    )
    return pd.concat([detail, top], ignore_index=True).drop_duplicates(
        ["code", "level", "newsletter"]
    )


def fec_id_map(legislators: pd.DataFrame) -> pd.DataFrame:
    """Explode the stringified `fec` list into one row per (cand_id, bioguide).

    A legislator can hold several FEC candidate IDs across a career, and money to any of
    them belongs to the same person.
    """
    rows = []
    for _, r in legislators.iterrows():
        raw = (r.get("fec") or "").strip()
        if not raw:
            continue
        try:
            ids = ast.literal_eval(raw)
        except (ValueError, SyntaxError):
            continue
        if isinstance(ids, str):
            ids = [ids]
        for cand_id in ids:
            if isinstance(cand_id, str) and cand_id.strip():
                rows.append({
                    "cand_id": cand_id.strip(),
                    "bioguide": r["bioguide"],
                    "legislator_name": r.get("official_full", ""),
                })
    return pd.DataFrame(rows).drop_duplicates("cand_id")


def current_memberships(membership: pd.DataFrame) -> pd.DataFrame:
    """Memberships for the latest congress only.

    The source spans several congresses; including all of them would count a member's
    committee history as current assignments and skew the even split across committees.
    """
    m = membership.copy()
    m["congress"] = pd.to_numeric(m["congress"], errors="coerce")
    latest = m["congress"].max()
    return m[m["congress"] == latest].drop_duplicates(["bioguide", "committee"])


PARTY_ABBREV = {"Republican": "R", "Democrat": "D", "Independent": "I",
                "Libertarian": "L", "Independent American": "I"}


def current_terms(terms: pd.DataFrame) -> pd.DataFrame:
    """One row per member: their CURRENT term's party, chamber, state and district.

    Party belongs to a term rather than a person -- members change party -- so the
    latest term by start date is the member's present standing.
    """
    t = terms.copy()
    t["start"] = pd.to_datetime(t["start"], errors="coerce")
    t = t.sort_values("start").drop_duplicates("bioguide", keep="last")
    t["party_short"] = t["party"].map(PARTY_ABBREV).fillna(t["party"].str[:1].str.upper())
    t["chamber"] = t["type"].map({"rep": "Rep.", "sen": "Sen."}).fillna("")
    dist = pd.to_numeric(t["district"], errors="coerce")
    t["seat"] = t["state"] + dist.map(lambda d: f"-{int(d):02d}" if pd.notna(d) else "")
    return t[["bioguide", "party", "party_short", "chamber", "state", "seat"]]


def candidate_newsletter_weights(
    legislators: pd.DataFrame, membership: pd.DataFrame, mapping: pd.DataFrame | None = None,
    terms: pd.DataFrame | None = None
) -> pd.DataFrame:
    """(cand_id, newsletter, weight) with weights summing to 1.0 per candidate.

    Two-level resolution, per committee the member sits on:

    * If they hold at least one **mapped subcommittee** seat in that committee, only those
      subcommittees count — the sharper signal wins.
    * Otherwise the committee's derived top-level union applies.

    Weighting is the agreed even split: each contributing committee gets an equal share,
    and that share divides equally among the distinct newsletters the member reaches
    through it. A member on Agriculture (1 newsletter) and Energy & Commerce (5) splits
    50/50 between the committees, not 1/6 vs 5/6 — otherwise a broad-jurisdiction committee
    would dominate purely by being broad.

    Committees that map to nothing are dropped **before** normalising, so money
    redistributes across the member's industry-relevant seats rather than evaporating. A
    member reaching no mapped committee at all yields no rows and is reported as uncovered.
    """
    cmte_nl = committee_newsletters(mapping)
    if cmte_nl.empty:
        return pd.DataFrame(columns=["cand_id", "newsletter", "weight"])

    memb = current_memberships(membership)
    joined = memb.merge(cmte_nl, left_on="committee", right_on="code", how="inner",
                        suffixes=("_seat", ""))

    # Prefer subcommittee rows: within each (member, parent committee), drop the derived
    # top-level fallback whenever a mapped subcommittee seat is present.
    has_sub = (
        joined[joined["level"] == "subcommittee"]
        .groupby(["bioguide", "committee"]).size().rename("n_sub")
    )
    joined = joined.join(has_sub, on=["bioguide", "committee"])
    joined = joined[(joined["level"] == "subcommittee") | joined["n_sub"].isna()]

    # Distinct newsletters the member reaches through each committee.
    reach = joined[["bioguide", "committee", "newsletter"]].drop_duplicates()

    n_cmte = reach.groupby("bioguide")["committee"].transform("nunique")
    n_nl = reach.groupby(["bioguide", "committee"])["newsletter"].transform("size")
    reach["weight"] = (1.0 / n_cmte) * (1.0 / n_nl)

    fec = fec_id_map(legislators)
    out = reach.merge(fec, on="bioguide", how="inner")
    out = (
        out.groupby(["cand_id", "bioguide", "legislator_name", "newsletter"], as_index=False)["weight"]
        .sum()
    )
    if terms is not None:
        out = out.merge(current_terms(terms), on="bioguide", how="left")
    return out


def coverage(weights: pd.DataFrame, legislators: pd.DataFrame, membership: pd.DataFrame) -> dict:
    """Where the three-hop join loses people — reported on every run."""
    fec = fec_id_map(legislators)
    memb = current_memberships(membership)
    mapped = set(weights["bioguide"]) if len(weights) else set()
    return {
        "legislators": len(legislators),
        "with_fec_id": fec["bioguide"].nunique(),
        "with_current_membership": memb["bioguide"].nunique(),
        "with_mapped_committee": len(mapped),
        "no_mapped_committee": memb["bioguide"].nunique() - len(mapped),
        "cand_ids_attributable": weights["cand_id"].nunique() if len(weights) else 0,
    }
