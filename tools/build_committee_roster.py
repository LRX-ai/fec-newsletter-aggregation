#!/usr/bin/env python3
"""The senior members of every committee a channel covers, and who funds them.

The receive edition ranks five members by money. That answers "who got the most" and
not "who sits where", which is the question a policy reader actually has: the chair of
the subcommittee writing crypto rules matters whether or not she led the quarter on
dollars. This builds the roster instead -- the ten highest-ranking members of every
committee and subcommittee the channel covers, with what each collected in the quarter
and who their largest backer was.

Rank is the committee's own seniority order from the unitedstates/congress-legislators
dataset, and it runs SEPARATELY DOWN EACH PARTY: rank 1 is both the chair and the
ranking member, rank 2 the next most senior on each side, and so on. Taking the top ten
therefore returns roughly five from each party, which is a property of the source, not a
balancing choice made here.

Money here is ALL support money reaching that member in the quarter, not just the
channel's share. A member's channel-attributed figure is a fraction of a fraction --
split across their committees and again across each committee's channels -- which is
the right unit for a channel total and a meaningless one beside a person's name.

Emits cache/committee_roster_<channel>_<period>.json -- the period is in the name
because a monthly edition and a quarterly one are different datasets for the same
channel and must not overwrite each other.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from periods import period_mask
from fec_newsletter import common, config, congress, extract, sources

TOP_N = 10
NAME_DROP = {"JR", "SR", "II", "III", "IV", "V", "MR", "MRS", "MS", "DR"}


def person(name: str) -> str:
    name = re.sub(r'"[^"]*"', " ", str(name))
    if "," in name:
        last, first = name.split(",", 1)
        parts = [w for w in first.replace(".", " ").split() if w.upper() not in NAME_DROP]
        name = " ".join(parts + [last.strip()])
    return " ".join(w for w in name.title().split() if w.upper() not in NAME_DROP)


def unit_labels(mapping: pd.DataFrame) -> dict[str, str]:
    """code -> readable label. A parent code takes the committee name from any of its
    rows; a subcommittee code appends its own name."""
    parent = {}
    for _, r in mapping.iterrows():
        if str(r.get("name") or "").strip():
            parent.setdefault(str(r["thomas_id"]).strip(), str(r["name"]).strip())
    out = dict(parent)
    for _, r in mapping.iterrows():
        sc = str(r.get("subcommittee_thomas_id") or "").strip()
        if not sc:
            continue
        code = str(r["thomas_id"]).strip() + sc.zfill(2)
        base = parent.get(str(r["thomas_id"]).strip(), str(r["thomas_id"]).strip())
        out[code] = f'{base} — {str(r.get("subcommittee_name") or "").strip()}'
    return out


def _slug(c):
    return re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="Finance & Insurance")
    ap.add_argument("--period", default=config.DEFAULT_PERIOD,
                    help=f"a month (2026-06) or a quarter (2026Q2); "
                         f"default {config.DEFAULT_PERIOD}")
    ap.add_argument("--top", type=int, default=TOP_N)
    args = ap.parse_args()

    data = extract.load({n: config.CACHE_DIR / c for n, (_, c) in config.EXTRACTS.items()})

    cov = common.Coverage()
    txns = sources.dedupe(pd.concat([sources.from_oth(data["oth"], cov),
                                     sources.from_pas2(data["pas2"], cov)], ignore_index=True), cov)
    txns = common.add_quarter(txns, cov, config.SEND_QUARTER_START, config.SEND_QUARTER_END)
    txns["cand"] = txns["recip_cand_id"].fillna(txns["recip_id"])
    q = txns[period_mask(txns, args.period)
             & (txns["support_oppose"] == "support")
             & (~txns["flow_type"].isin(["loan", "loan_repayment"]))]

    cm = data["committees"].drop_duplicates("CMTE_ID").set_index("CMTE_ID")["CMTE_NM"]
    fecmap = congress.fec_id_map(data["legislators"])
    cand_by_bg = fecmap.groupby("bioguide")["cand_id"].apply(set).to_dict()

    terms = congress.current_terms(data["terms"])
    terms = terms.set_index("bioguide") if "bioguide" in terms.columns else terms
    leg = data["legislators"].drop_duplicates("bioguide").set_index("bioguide")
    namecol = next((c for c in ("official_full", "full_name", "name") if c in leg.columns), None)

    cn = congress.committee_newsletters()
    attributed = set(cn.loc[cn["newsletter"] == args.channel, "code"])
    ranks = congress.committee_ranks(args.channel)
    # Rank order, so the table reads down from the committees that define the channel.
    codes = [c for c in sorted(ranks, key=ranks.__getitem__) if c in attributed]
    if not codes:
        raise SystemExit(f"no ranked committees for {args.channel!r} in "
                         f"{config.COMMITTEE_RANKS.name}")
    n_dropped = len(attributed) - len(codes)
    labels = unit_labels(congress.load_mapping())
    memb = pd.read_csv(config.CACHE_DIR / "committee_membership.csv", dtype=str)
    memb["rank"] = pd.to_numeric(memb["rank"], errors="coerce")

    money_cache: dict[str, dict] = {}

    def money_for(bg: str) -> dict:
        if bg in money_cache:
            return money_cache[bg]
        ids = cand_by_bg.get(bg, set())
        sub = q[q["cand"].isin(ids)]
        if sub.empty:
            r = {"total": 0, "backers": 0, "top": "", "top_share": 0}
        else:
            g = sub.groupby("sender_id")["amount"].sum().sort_values(ascending=False)
            tot = float(g.sum())
            # A member whose refunds outrun their receipts nets negative for the quarter.
            # The net is reported as filed, but the largest backer is taken from the
            # committees that actually GAVE -- a share of a negative total is nonsense,
            # and the largest giver is still a real fact about the quarter.
            pos = g[g > 0]
            r = {"total": round(tot), "backers": int(pos.size),
                 "top": str(cm.get(pos.index[0], pos.index[0])) if len(pos) else "",
                 "top_share": round(100 * pos.iloc[0] / pos.sum()) if len(pos) else 0,
                 "gave": round(float(pos.sum())) if len(pos) else 0}
        money_cache[bg] = r
        return r

    rows = []
    for code in codes:
        sub = memb[memb["committee"] == code].nsmallest(args.top, "rank")
        if sub.empty:
            continue
        for _, r in sub.iterrows():
            bg = r["bioguide"]
            t = terms.loc[bg] if bg in terms.index else {}
            # congress.current_terms already assembles "WA-01" / "AK"; rebuilding it from
            # a district column that does not exist silently produced bare states.
            seat = ""
            if len(t):
                seat = str(t.get("seat") or t.get("state") or "")
                if str(t.get("chamber") or "").lower().startswith("sen"):
                    seat = f"{seat}-Sen"
            nm = str(leg.loc[bg, namecol]) if bg in leg.index and namecol else bg
            rows.append({
                "code": code, "unit": labels.get(code, code),
                "chamber": "Senate" if code.startswith("S") else "House",
                "rank": int(r["rank"]) if pd.notna(r["rank"]) else 99,
                "title": str(r["title"]) if pd.notna(r["title"]) else "",
                "bioguide": bg, "name": person(nm),
                "party": str(t.get("party") or "")[:1] if len(t) else "",
                "seat": seat, **money_for(bg)})

    # Committees in the channel's policy-rank order, members by seniority within each.
    # The rank itself is deliberately not carried onto the row: it orders the table and
    # is not a column in it.
    rows.sort(key=lambda x: (ranks[x["code"]], x["rank"]))
    out = {"channel": args.channel, "quarter": args.period, "units": len(set(r["code"] for r in rows)),
           "rows": len(rows), "members": len(set(r["bioguide"] for r in rows)),
           "units_excluded": n_dropped, "data": rows}
    # The period is in the filename: a monthly edition and a quarterly one are
    # different datasets for the same channel and must not overwrite each other.
    p = config.REPO_ROOT / "cache" / f"committee_roster_{_slug(args.channel)}_{_slug(args.period)}.json"
    p.write_text(json.dumps(out))
    paid = [r for r in rows if r["total"] > 0]
    print(f"{out['units']} committees, {out['rows']} seats, {out['members']} members -> "
          f"{p.name} ({p.stat().st_size/1024:.0f} KB)")
    print(f"  committees the channel attributes through but the ranking excludes: {n_dropped}")
    print(f"  seats whose holder took money this quarter: {len(paid)} "
          f"({100*len(paid)/len(rows):.0f}%)")
    print(f"  chairs and ranking members: {sum(1 for r in rows if r['title'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
