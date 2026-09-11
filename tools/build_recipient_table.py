#!/usr/bin/env python3
"""Every politician a channel's PACs paid in one quarter, as one table.

The send edition ranks six PACs and names one recipient each. That is a fair summary
of who GAVE and a very poor account of who RECEIVED: the same quarter reaches 532
candidates, and the top twenty of them hold only 22% of the money. A ranked-six view
cannot show a long tail, and the long tail is most of the story.

Emits cache/recipient_table.json for the send edition's dashboard.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from periods import period_mask
from fec_newsletter import config, congress, extract, pipelines

NAME_DROP = {"JR","SR","II","III","IV","V","MR","MRS","MS","DR","SEN","REP","HON"}
ICI = {"I": "incumbent", "C": "challenger", "O": "open seat"}


def person(name: str) -> str:
    name = re.sub(r'"[^"]*"', " ", str(name))
    if "," in name:
        last, first = name.split(",", 1)
        parts = [w for w in first.replace(".", " ").split() if w.upper() not in NAME_DROP]
        name = " ".join(parts + [last.strip()])
    return " ".join(w for w in name.title().split() if w.upper() not in NAME_DROP)


def seat(row) -> str:
    st = str(row.get("CAND_OFFICE_ST") or "")
    if str(row.get("CAND_OFFICE")) == "S":
        return f"{st}-Sen"
    d = str(row.get("CAND_OFFICE_DISTRICT") or "").strip()
    return f"{st}-{d.zfill(2)}" if d and d.lower() != "nan" else st


def _slug(c):
    return re.sub(r"[^a-z0-9]+", "_", c.lower()).strip("_")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="Finance & Insurance")
    ap.add_argument("--period", default=config.DEFAULT_PERIOD,
                    help=f"a month (2026-06) or a quarter (2026Q2); "
                         f"default {config.DEFAULT_PERIOD}")
    args = ap.parse_args()

    data = extract.load({n: config.CACHE_DIR / c for n, (_, c) in config.EXTRACTS.items()})
    send, _ = pipelines.run_send(data)
    s = send[(send["newsletter"] == args.channel) & period_mask(send, args.period)]
    a = s[s["counterparty_id"].astype(str).str[:1].isin(list("HSP"))].copy()

    races = pd.read_csv(config.CACHE_DIR / "candidate_races.csv", dtype=str).fillna("")
    races = races.sort_values("CAND_ELECTION_YR").drop_duplicates("CAND_ID", keep="last").set_index("CAND_ID")

    # Whether the recipient also SITS on a committee this channel covers. This is the
    # one column that joins the two editions: a candidate who appears here and holds
    # the seat is being paid by the industry whose rules they write.
    weights = congress.candidate_newsletter_weights(
        data["legislators"], data["membership"], terms=data.get("terms"))
    on_seat = set(weights.loc[weights["newsletter"] == args.channel, "cand_id"])
    any_seat = set(weights["cand_id"])

    rows = []
    for cid, sub in a.groupby("counterparty_id"):
        total = sub["amount"].sum()
        by_pac = sub.groupby("attributed_name")["amount"].sum().sort_values(ascending=False)
        pgi = sub["transaction_pgi"].fillna("").str.upper()
        r = races.loc[cid] if cid in races.index else {}
        name = person(r.get("CAND_NAME") or sub["counterparty_name"].iloc[0])
        rows.append({
            "id": cid,
            "name": name,
            "party": str(r.get("CAND_PTY_AFFILIATION") or "")[:3],
            "seat": seat(r) if len(r) else "",
            "chamber": "Senate" if str(r.get("CAND_OFFICE")) == "S" else "House",
            "status": ICI.get(str(r.get("CAND_ICI") or ""), "unknown"),
            "total": round(float(total)),
            "pacs": int(sub["attributed_name"].nunique()),
            "top_pac": by_pac.index[0],
            "top_share": round(100 * by_pac.iloc[0] / total) if total else 0,
            "primary": round(float(sub.loc[pgi.str.startswith("P"), "amount"].sum())),
            "general": round(float(sub.loc[pgi.str.startswith("G"), "amount"].sum())),
            "seat_in_channel": cid in on_seat,
            "seat_any": cid in any_seat,
        })
    rows.sort(key=lambda x: -x["total"])
    out = {"channel": args.channel, "quarter": args.period,
           "candidates": len(rows), "senders": int(a["attributed_name"].nunique()),
           "total": round(float(a["amount"].sum())), "rows": rows}
    # The period is in the filename: a monthly edition and a quarterly one are
    # different datasets for the same channel and must not overwrite each other.
    p = config.REPO_ROOT / "cache" / f"recipient_table_{_slug(args.channel)}_{_slug(args.period)}.json"
    p.write_text(json.dumps(out))
    print(f"{len(rows)} recipients, ${out['total']:,} -> {p.name} ({p.stat().st_size/1024:.0f} KB)")
    seated = [r for r in rows if r["seat_in_channel"]]
    print(f"  sit on a {args.channel} committee: {len(seated)}  ${sum(r['total'] for r in seated):,}")
    print(f"  sit on some mapped committee   : {sum(1 for r in rows if r['seat_any'])}")
    print(f"  negative (net refund)          : {sum(1 for r in rows if r['total'] < 0)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
