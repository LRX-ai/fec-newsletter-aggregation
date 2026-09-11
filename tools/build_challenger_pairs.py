#!/usr/bin/env python3
"""Finance-committee incumbents paired against the money funding their challengers.

The receive edition's blind spot is structural: it finds a member through the committee
seat they hold, so a candidate trying to take that seat is invisible to it. Section B
recovers five such races by hand. This recovers all of them -- every seat held by a
member this channel covers, every non-incumbent contesting it who raised or drew money
this quarter, and who supplied it.

The gap is not small. The channel sees $14.0M reaching the members it covers; the seats
those members hold have $130.3M of non-incumbent money moving around them in the same
quarter, most of it invisible to any committee-seat attribution.

Both sides are measured the same way -- ALL support money in the quarter, whoever sent
it -- because a comparison between "what the finance channel gave the incumbent" and
"what anyone gave the challenger" would not be a comparison at all. The incumbent's
channel-attributed figure is carried alongside so the row still ties to the edition.

Emits cache/challenger_pairs.json.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from periods import period_mask
from fec_newsletter import common, config, congress, extract, pipelines, sources

NAME_DROP = {"JR","SR","II","III","IV","V","MR","MRS","MS","DR","SEN","REP","HON"}
ICI = {"I": "incumbent", "C": "challenger", "O": "open seat"}


def person(name: str) -> str:
    name = re.sub(r'"[^"]*"', " ", str(name))
    if "," in name:
        last, first = name.split(",", 1)
        parts = [w for w in first.replace(".", " ").split() if w.upper() not in NAME_DROP]
        name = " ".join(parts + [last.strip()])
    return " ".join(w for w in name.title().split() if w.upper() not in NAME_DROP)


def seat_of(row) -> str:
    st = str(row["CAND_OFFICE_ST"])
    if str(row["CAND_OFFICE"]) == "S":
        return f"{st}-Sen"
    d = str(row["CAND_OFFICE_DISTRICT"] or "").strip()
    return f"{st}-{d.zfill(2)}" if d and d.lower() != "nan" else st


def _slug(c):
    return re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="Finance & Insurance")
    ap.add_argument("--period", default=config.DEFAULT_PERIOD,
                    help=f"a month (2026-06) or a quarter (2026Q2); "
                         f"default {config.DEFAULT_PERIOD}")
    ap.add_argument("--min", type=float, default=1000,
                    help="omit challengers with less than this much money in play")
    args = ap.parse_args()

    data = extract.load({n: config.CACHE_DIR / c for n, (_, c) in config.EXTRACTS.items()})
    recv, _ = pipelines.run_receive(data)
    r = recv[(recv["newsletter"] == args.channel)
             & period_mask(recv, args.period)]
    chan_by_cand = r.groupby("cand_id")["amount"].sum().to_dict()

    cov = common.Coverage()
    t = sources.dedupe(pd.concat([sources.from_oth(data["oth"], cov),
                                  sources.from_pas2(data["pas2"], cov)], ignore_index=True), cov)
    t = common.add_quarter(t, cov, config.SEND_QUARTER_START, config.SEND_QUARTER_END)
    t["cand"] = t["recip_cand_id"].fillna(t["recip_id"])
    q = t[period_mask(t, args.period)
          & (~t["flow_type"].isin(["loan", "loan_repayment"]))]
    cm = data["committees"].drop_duplicates("CMTE_ID").set_index("CMTE_ID")["CMTE_NM"]

    def stance(cid, so):
        sub = q[(q["cand"] == cid) & (q["support_oppose"] == so)]
        if sub.empty:
            return 0.0, "", 0, 0
        g = sub.groupby("sender_id")["amount"].sum().sort_values(ascending=False)
        return (float(g.sum()), str(cm.get(g.index[0], g.index[0])),
                round(100 * g.iloc[0] / g.sum()) if g.sum() else 0, int(g.size))

    races = pd.read_csv(config.CACHE_DIR / "candidate_races.csv", dtype=str).fillna("")
    races = races.sort_values("CAND_ELECTION_YR").drop_duplicates("CAND_ID", keep="last")
    members = set(chan_by_cand)
    mine = races[races["CAND_ID"].isin(members)]

    # How much each incumbent matters to this channel, by the committees they sit on.
    # Section E orders on this ALONE, with no money in it: the section exists to show the
    # money attribution cannot see, so ordering it by money would rank seats by the very
    # quantity the section is there to say is missing.
    _imp = congress.member_importance(data["membership"], args.channel).set_index("bioguide")
    _bg = dict(zip(congress.fec_id_map(data["legislators"])["cand_id"],
                   congress.fec_id_map(data["legislators"])["bioguide"]))

    def importance_of(cand_id):
        row = _imp.loc[_bg[cand_id]] if _bg.get(cand_id) in _imp.index else None
        if row is None:
            return 0.0, 0, 0
        return float(row["importance"]), int(row["seats"]), int(row["best_rank"])
    key = ["CAND_OFFICE", "CAND_OFFICE_ST", "CAND_OFFICE_DISTRICT"]

    rows = []
    for _, inc in mine.iterrows():
        contenders = races[(races[key[0]] == inc[key[0]]) & (races[key[1]] == inc[key[1]])
                           & (races[key[2]] == inc[key[2]])
                           & (races["CAND_ID"] != inc["CAND_ID"])
                           & (~races["CAND_ID"].isin(members))]
        if contenders.empty:
            continue
        inc_sup, inc_top, inc_share, _ = stance(inc["CAND_ID"], "support")
        inc_imp, inc_seats, inc_best = importance_of(inc["CAND_ID"])
        for _, ch in contenders.iterrows():
            sup, top, share, backers = stance(ch["CAND_ID"], "support")
            opp, opp_top, _, _ = stance(ch["CAND_ID"], "oppose")
            if sup + opp < args.min:
                continue
            rows.append({
                "seat": seat_of(inc), "chamber": "Senate" if inc["CAND_OFFICE"] == "S" else "House",
                "incumbent": person(inc["CAND_NAME"]),
                "inc_party": str(inc["CAND_PTY_AFFILIATION"])[:1],
                "inc_support": round(inc_sup),
                "inc_channel": round(float(chan_by_cand.get(inc["CAND_ID"], 0))),
                "inc_importance": round(inc_imp, 4),
                "inc_seats": inc_seats, "inc_best_rank": inc_best,
                "challenger": person(ch["CAND_NAME"]),
                "ch_party": str(ch["CAND_PTY_AFFILIATION"])[:1],
                "ch_status": ICI.get(str(ch["CAND_ICI"]), "unknown"),
                "ch_support": round(sup), "ch_backers": backers,
                "ch_top": top, "ch_top_share": share,
                "ch_opposed": round(opp), "ch_opp_top": opp_top,
                "ratio": round(sup / inc_sup, 2) if inc_sup > 0 else None,
            })
    # Grouped by incumbent, not by pair: several challengers contesting the same seat
    # belong together, and sorting the flat pair list by money scattered them. Groups now
    # rank by the INCUMBENT'S IMPORTANCE to the channel rather than by the money
    # contesting the seat -- the seat worth reading about first is the one whose holder
    # writes the most of this channel's law, whether or not anyone is outspending them.
    # Within a group, rank on BACKING rather than total money in play: the prominent
    # column is what the challenger raised, and a $15k row sitting above a $250k one
    # because of opposition spending reads as a sorting bug.
    rows.sort(key=lambda x: (-x["inc_importance"], x["seat"], x["incumbent"],
                             -x["ch_support"], -x["ch_opposed"]))
    out = {"channel": args.channel, "quarter": args.period, "pairs": len(rows),
           "seats": len({r["seat"] for r in rows}),
           "challengers": len({(r["seat"], r["challenger"]) for r in rows}),
           "ch_money": sum(r["ch_support"] for r in rows),
           "opp_money": sum(r["ch_opposed"] for r in rows),
           "channel_total": round(float(r["amount"].sum())) if len(r) else 0,
           "rows": rows}
    # The period is in the filename: a monthly edition and a quarterly one are
    # different datasets for the same channel and must not overwrite each other.
    p = config.REPO_ROOT / "cache" / f"challenger_pairs_{_slug(args.channel)}_{_slug(args.period)}.json"
    p.write_text(json.dumps(out))
    print(f"{out['pairs']} pairs across {out['seats']} seats, {out['challengers']} challengers "
          f"-> {p.name} ({p.stat().st_size/1024:.0f} KB)")
    print(f"  money supporting challengers : ${out['ch_money']:,}")
    print(f"  money spent against them     : ${out['opp_money']:,}")
    beat = [x for x in rows if x["ratio"] and x["ratio"] > 1]
    print(f"  challengers out-raising their incumbent: {len(beat)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
