"""FEC candidate -> congressional committee -> newsletter.

This is what the receive pipeline attributes from. A candidate has no catcode, so the
industry signal comes from the committees they sit on: money reaching a member of House
Agriculture is money reaching agricultural policy.

The join is three hops, and each one loses rows:

    FEC CAND_ID  ->  bioguide          via c_github_legislators_current_person.fec
    bioguide     ->  subcommittee code via c_github_committee_membership_current
    subcommittee ->  newsletter(s)     via congress_cmte_crosswalk.csv

The crosswalk's newsletter labels are hand-authored, originally in
https://docs.google.com/spreadsheets/d/1mq0-IUKbm7TmbmuXfyOoaxVSBCdYWSZXqYF-NBY4hHQ/edit?gid=28307445
(Bella's work). `tools/audit_crosswalk.py` re-derives which of those labels deserve a
second look, and prices each one by the money that rests on it alone.

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

# A committee whose jurisdiction is purely procedural -- the ethics committees, Printing,
# the Library -- has no industry to attribute to, and that is a decision rather than an
# omission. Writing this sentinel in the newsletters column records the decision, so a
# blank cell means "not yet looked at" and nothing else.
NO_MAPPING = frozenset({"none"})

MAPPING_COLUMNS = [
    "name", "thomas_id", "jurisdiction_source", "subcommittee_name",
    "subcommittee_thomas_id", "newsletters", "notes",
]


def _label_key(label: str) -> str:
    return re.sub(r"[^a-z0-9]", "", label.lower())


def _normalise_label(label: str) -> tuple[str, ...]:
    return LABEL_ALIASES.get(_label_key(label), ())


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
            if part and not _normalise_label(part) and _label_key(part) not in NO_MAPPING:
                seen.add(part)
    return seen


def load_mapping() -> pd.DataFrame:
    """The authored subcommittee -> newsletter crosswalk.

    Read by header name, not by position: the file is hand-edited in a spreadsheet, and a
    positional read turns a reordered or inserted column into silently wrong labels rather
    than an error. A missing expected column is an error; extra columns are ignored.
    """
    path = config.COMMITTEE_NEWSLETTER_MAP
    if not path.exists():
        raise FileNotFoundError(f"committee->newsletter mapping not found at {path}")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    missing = [c for c in MAPPING_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"{path.name} is missing column(s): {', '.join(missing)}")
    return df[MAPPING_COLUMNS]


def committee_newsletters(mapping: pd.DataFrame | None = None) -> pd.DataFrame:
    """(code, level, newsletter) for every mapped subcommittee, plus a top-level row.

    The top-level rows are the fallback for a member who sits on a committee but holds no
    mapped subcommittee seat there: they receive the UNION of that committee's subcommittee
    newsletters, derived from the same file rather than hand-written, so there is one
    authorship and one source of truth.

    A committee with no subcommittees of its own -- the budget committees, Senate
    Intelligence, the joint committees -- has nothing to derive from, so its label is
    authored directly on the row whose subcommittee columns are blank. An authored label
    REPLACES the derived union rather than adding to it: it is a deliberate statement about
    the whole committee, and a reader comparing the file to the output should not have to
    union two things in their head to predict it.

    A top-level label is far more leveraged than a subcommittee one -- every member of the
    committee receives it, with no sharper seat to outrank it -- so these are kept to one or
    two newsletters. Rows carrying the NO_MAPPING sentinel produce nothing, on purpose.
    """
    mapping = load_mapping() if mapping is None else mapping
    is_sub = mapping["subcommittee_thomas_id"].str.strip() != ""

    rows = []
    for _, r in mapping[is_sub].iterrows():
        code = r["thomas_id"].strip() + r["subcommittee_thomas_id"].strip().zfill(2)
        for nl in parse_newsletters(r["newsletters"], r["thomas_id"].strip()):
            rows.append({"code": code, "committee": r["thomas_id"].strip(),
                         "level": "subcommittee", "newsletter": nl})
    detail = pd.DataFrame(rows, columns=["code", "committee", "level", "newsletter"])

    authored = {}
    for _, r in mapping[~is_sub].iterrows():
        code = r["thomas_id"].strip()
        nls = parse_newsletters(r["newsletters"], code)
        if nls:
            authored[code] = nls

    top = [{"code": c, "committee": c, "level": "committee", "newsletter": nl}
           for c, nls in authored.items() for nl in nls]
    derived = detail[~detail["committee"].isin(authored)][["committee", "newsletter"]]
    top.extend(
        {"code": c, "committee": c, "level": "committee", "newsletter": nl}
        for c, nl in derived.drop_duplicates().itertuples(index=False)
    )
    if detail.empty and not top:
        return detail
    return pd.concat(
        [detail, pd.DataFrame(top, columns=detail.columns)], ignore_index=True
    ).drop_duplicates(["code", "level", "newsletter"])


def committee_ranks(newsletter: str) -> dict[str, int]:
    """code -> this newsletter's policy rank for it, 1 being the most central.

    The crosswalk says which committees a channel attributes money THROUGH. This says how
    much each of them matters to it, which is a different question and a hand-reviewed
    answer. A code missing from this file was judged not to belong to the channel at all,
    so its absence is a decision rather than a gap.
    """
    path = config.COMMITTEE_RANKS
    if not path.exists():
        raise FileNotFoundError(
            f"committee ranking not found at {path} -- run tools/build_category_ranks.py")
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    df = df[df["newsletter"] == newsletter]
    return {c: int(r) for c, r in zip(df["code"], df["rank"])}


def member_importance(membership: pd.DataFrame, newsletter: str) -> pd.DataFrame:
    """(bioguide, importance, seats, best_rank, best_seniority) -- how much a member
    matters to a channel.

    Each seat contributes the PRODUCT of two linear weights, and the seats are summed:

        seat weight = (N - committee_rank + 1)/N  x  (M - seniority_rank + 1)/M

    * committee_rank is the channel's own ranking of that committee, 1..N -- how much the
      committee matters to this policy area.
    * seniority_rank is the member's standing ON that committee, 1..M, from the source's
      own order. It runs separately down each party, so rank 1 is both the chair and the
      ranking member, and both are weighted as the top of their side.

    Multiplying is what makes the two questions one number: a junior seat on the channel's
    first-ranked committee and a chair's seat on its last both come out small, and a chair
    of the first-ranked committee comes out large. SUMMING the seats then makes seat COUNT
    count too, so sitting on four of a channel's committees beats sitting on one without a
    third knob to tune.

    Linear decay on both, not 1/rank: 1/rank makes rank 2 worth half of rank 1, which
    swamps every other seat a member holds.

    `relevance` is the same figure indexed 0-100 against the most relevant member of this
    channel, and it is the one to PRINT. The raw sum has no usable ceiling: each seat adds
    at most 1.0, so the theoretical maximum is the channel's committee count -- 28 for
    Defense -- while no real member passes 3. A reader shown "2.47" cannot tell whether
    that is exceptional, and the raw figure is not comparable between channels either,
    because a 7-committee channel cannot reach what a 27-committee one can. Indexing fixes
    both. Rankings are untouched: dividing a channel by its own maximum is monotonic.

    A member with no ranked seat scores 0, and that zero is a finding rather than a missing
    value: every committee they sit on was judged outside this channel. Callers ranking by
    importance should drop them rather than treat the zero as unknown.
    """
    ranks = committee_ranks(newsletter)
    n = len(ranks)
    cols = ["bioguide", "importance", "relevance", "seats", "best_rank", "best_seniority"]
    if not n:
        return pd.DataFrame(columns=cols)
    m = current_memberships(membership)
    m = m[m["committee"].isin(ranks)].copy()
    m["cmte_rank"] = m["committee"].map(ranks)
    # Seniority is scaled against the DEPTH OF THAT COMMITTEE's own list: rank 5 of 36 is
    # a senior seat and rank 5 of 6 is a junior one, and a fixed divisor would call them
    # the same thing.
    seniority = pd.to_numeric(m["rank"], errors="coerce").fillna(1)
    depth = seniority.groupby(m["committee"]).transform("max").clip(lower=1)
    m["weight"] = ((n - m["cmte_rank"] + 1) / n) * ((depth - seniority + 1) / depth)
    m["seniority"] = seniority
    out = m.groupby("bioguide").agg(
        importance=("weight", "sum"), seats=("committee", "nunique"),
        best_rank=("cmte_rank", "min"), best_seniority=("seniority", "min")).reset_index()
    # Indexed against this channel's own top member, so 100 is always someone real.
    top = out["importance"].max()
    out["relevance"] = (100 * out["importance"] / top).round().astype(int) if top else 0
    return out[cols]


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
