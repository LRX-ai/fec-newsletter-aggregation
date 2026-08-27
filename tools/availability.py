#!/usr/bin/env python3
"""Data-availability report: what can we publish, per category, per quarter --
and how much of the quarter's money never makes it there.

    python tools/availability.py                      # every quarter, by newsletter
    python tools/availability.py --quarter 2026Q2     # one quarter
    python tools/availability.py --by catcode         # CRP category code instead
    python tools/availability.py --csv out/avail.csv  # machine-readable

"Category" is ambiguous in this project, so --by covers all four readings:
  newsletter  the publishing channel (default)
  catcode     the CRP category code -- send only; receive has no catcode
  industry    CRP industry, from the crosswalk
  sector      CRP sector, from the crosswalk

Two numbers per quarter, and they answer different questions:

  THE FUNNEL is whole dollars. It measures the money in scope for the quarter
  against the money that reaches an output, and names where the rest goes. It is
  computed BEFORE multi-newsletter fan-out, so it never double-counts.

  THE CATEGORY ROWS are post-fan-out, matching out/*.csv. Under
  MULTI_NEWSLETTER="duplicate" the send column therefore sums to MORE than the
  funnel's attributed figure -- a catcode covering three newsletters contributes
  its full amount to each. That is expected, not a discrepancy.

The drops are measured between the real pipeline functions rather than
reimplemented, so this cannot drift from what the pipelines actually do.
"""
from __future__ import annotations
import argparse, pathlib, sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
import pandas as pd
from fec_newsletter import common, config, congress, extract, pipelines, sources

MIN_DOLLARS = 250_000
MIN_COUNTERPARTIES = 5


def _quarter(df: pd.DataFrame) -> pd.Series:
    return pd.to_datetime(df["transaction_dt"], format="%Y-%m-%d",
                          errors="coerce").dt.to_period("Q").astype(str)


def _by_q(df: pd.DataFrame, label: str) -> pd.Series:
    return df.groupby("quarter")["amount"].sum().rename(label)


def funnel(data: dict) -> pd.DataFrame:
    """Per-quarter whole-dollar funnel for both sides, with drop reasons.

    Quarter is attached immediately after source normalisation so every stage can
    be measured per quarter -- the pipelines themselves only compute it at the end.
    """
    cov = common.Coverage()
    oth = sources.from_oth(data["oth"], cov)
    pas2 = sources.from_pas2(data["pas2"], cov)
    combined = sources.dedupe(pd.concat([oth, pas2], ignore_index=True), cov)
    combined["quarter"] = _quarter(combined)
    combined = combined[combined["quarter"].notna()]

    parts = []

    # ---- SEND: everything in scope, attributed from the sender's catcode -------
    s = combined.copy()
    s_scope = _by_q(s, "send_in_scope")
    s1 = common.resolve_catcode(s, "sender_id", data["committees"],
                                data["crp_cmte_mapping"], cov)
    no_cat = _by_q(s1[s1["catcode"].isna()], "send_drop_no_catcode")
    s1 = s1[s1["catcode"].notna()]
    s2 = common.attach_newsletter(s1, data["crosswalk"], cov)
    no_nl = _by_q(s1[~s1["sub_id"].isin(set(s2["sub_id"]))], "send_drop_no_newsletter")
    parts += [s_scope, _by_q(s2, "send_attributed"), no_cat, no_nl]

    # ---- RECEIVE: candidate money, attributed from committee seats -------------
    r = combined.copy()
    r["cand_id"] = r["recip_cand_id"].fillna(r["recip_id"])
    r_cand = r[r["cand_id"].str[:1].isin(["H", "S", "P"])]
    parts.append(_by_q(r_cand, "recv_in_scope"))
    parts.append(_by_q(r[~r["sub_id"].isin(set(r_cand["sub_id"]))],
                       "recv_drop_not_to_candidate"))
    w = congress.candidate_newsletter_weights(data["legislators"], data["membership"])
    covered = set(w["cand_id"])
    hit = r_cand[r_cand["cand_id"].isin(covered)]
    parts.append(_by_q(hit, "recv_attributed"))
    parts.append(_by_q(r_cand[~r_cand["cand_id"].isin(covered)],
                       "recv_drop_no_committee_seat"))

    return pd.concat(parts, axis=1).fillna(0.0)


def label_columns(df: pd.DataFrame, crosswalk: pd.DataFrame, by: str):
    """Attach the requested category column, or None if this side cannot supply it."""
    if by == "newsletter":
        return df.assign(category=df["newsletter"])
    if "catcode" not in df.columns:
        return None
    if by == "catcode":
        return df.assign(category=df["catcode"])
    xw = crosswalk.copy()
    xw["catcode"] = common.norm_catcode(xw["catcode"])
    return df.assign(category=df["catcode"].map(
        xw.drop_duplicates("catcode").set_index("catcode")[by]))


def summarise(df: pd.DataFrame, side: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["quarter", "category", f"{side}_rows",
                                     f"{side}_amount", f"{side}_parties"])
    g = (df.groupby(["quarter", "category"], dropna=False)
           .agg(**{f"{side}_rows": ("amount", "size"),
                   f"{side}_amount": ("amount", "sum"),
                   f"{side}_parties": ("counterparty_id", "nunique")}).reset_index())
    g["quarter"] = g["quarter"].astype(str)
    return g


def verdict(amount: float, parties: int) -> str:
    if not amount or parties == 0:
        return "NONE"
    return "THIN" if amount < MIN_DOLLARS or parties < MIN_COUNTERPARTIES else "OK"


def pct(part: float, whole: float) -> str:
    return f"{part / whole * 100:5.1f}%" if whole else "    -"


def reconcile(fun: pd.DataFrame, send: pd.DataFrame, recv: pd.DataFrame) -> int:
    """Prove the funnel against the pipelines it is measuring.

    Receive must match to the dollar: its weights sum to 1.0 per candidate, so
    weighting redistributes money without creating or destroying any. Send is
    measured pre-fan-out, so it must be LOWER than the pipeline by exactly the
    multi-newsletter duplication factor -- never higher.
    """
    ok = True
    rq = recv.assign(q=recv["quarter"].astype(str)).groupby("q")["amount"].sum()
    sq = send.assign(q=send["quarter"].astype(str)).groupby("q")["amount"].sum()

    print(f"{'quarter':<9}{'recv funnel':>15}{'recv pipeline':>15}{'diff':>8}"
          f"{'send funnel':>15}{'send pipeline':>15}{'fan-out':>9}")
    for q in sorted(rq.index):
        rf = fun.loc[q, "recv_attributed"] if q in fun.index else 0.0
        sf = fun.loc[q, "send_attributed"] if q in fun.index else 0.0
        diff = rf - rq[q]
        if abs(diff) > 1:
            ok = False
        if sf and sq[q] < sf - 1:
            ok = False
        print(f"{q:<9}{rf:>15,.0f}{rq[q]:>15,.0f}{diff:>8,.0f}"
              f"{sf:>15,.0f}{sq[q]:>15,.0f}{(sq[q]/sf if sf else 0):>8.2f}x")

    for q in fun.index:
        r = fun.loc[q]
        if abs(r.send_attributed + r.send_drop_no_catcode
               + r.send_drop_no_newsletter - r.send_in_scope) > 1:
            ok = False; print(f"  send drops do not sum to in-scope for {q}")
        if abs(r.recv_attributed + r.recv_drop_no_committee_seat - r.recv_in_scope) > 1:
            ok = False; print(f"  receive drops do not sum to in-scope for {q}")

    print("\n[ok] funnel reconciles with both pipelines" if ok
          else "\n[FAIL] funnel does not reconcile")
    return 0 if ok else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--quarter")
    ap.add_argument("--by", default="newsletter",
                    choices=["newsletter", "catcode", "industry", "sector"])
    ap.add_argument("--csv", type=pathlib.Path)
    ap.add_argument("--reconcile", action="store_true",
                    help="verify the funnel against the pipelines and exit")
    args = ap.parse_args()

    paths = {n: config.CACHE_DIR / c for n, (_, c) in config.EXTRACTS.items()}
    if [n for n, p in paths.items() if not p.exists()]:
        print("missing cached extracts; run `python run_send.py` first", file=sys.stderr)
        return 1
    data = extract.load(paths)

    fun = funnel(data)
    send, _ = pipelines.run_send(data)
    recv, _ = pipelines.run_receive(data)

    if args.reconcile:
        return reconcile(fun, send, recv)

    s = summarise(label_columns(send, data["crosswalk"], args.by), "send")
    recv_lab = label_columns(recv, data["crosswalk"], args.by)
    recv_available = recv_lab is not None
    tbl = (s.merge(summarise(recv_lab, "recv"), on=["quarter", "category"], how="outer")
           if recv_available else s.assign(recv_rows=0, recv_amount=0.0, recv_parties=0))
    tbl = tbl.fillna({"send_rows": 0, "send_amount": 0, "send_parties": 0,
                      "recv_rows": 0, "recv_amount": 0, "recv_parties": 0})
    tbl = tbl[tbl["category"].notna()]

    quarters = [args.quarter] if args.quarter else sorted(set(tbl["quarter"]))
    if args.quarter and args.quarter not in set(tbl["quarter"]):
        print(f"no data for {args.quarter}. available: "
              f"{', '.join(sorted(set(tbl['quarter'])))}", file=sys.stderr)
        return 1

    tbl["send"] = [verdict(a, p) for a, p in zip(tbl.send_amount, tbl.send_parties)]
    tbl["recv"] = [verdict(a, p) for a, p in zip(tbl.recv_amount, tbl.recv_parties)]
    tbl = tbl.sort_values(["quarter", "send_amount"], ascending=[True, False])

    if not recv_available:
        print(f"\nNote: receive is not reportable by {args.by} — it attributes from "
              f"congressional committee seats, not catcodes. Send column only.")

    for q in quarters:
        chunk = tbl[tbl["quarter"] == q]
        f = fun.loc[q] if q in fun.index else None
        print(f"\n{'=' * 78}\n{q}\n{'=' * 78}")

        if f is not None:
            print("Whole dollars in scope this quarter, and where they go:\n")
            print(f"  SEND     in scope            ${f.send_in_scope:>15,.0f}")
            print(f"           attributed          ${f.send_attributed:>15,.0f}  "
                  f"{pct(f.send_attributed, f.send_in_scope)}")
            print(f"           dropped: no catcode ${f.send_drop_no_catcode:>15,.0f}  "
                  f"{pct(f.send_drop_no_catcode, f.send_in_scope)}   "
                  f"sender's industry unknown")
            print(f"                    no channel ${f.send_drop_no_newsletter:>15,.0f}  "
                  f"{pct(f.send_drop_no_newsletter, f.send_in_scope)}   "
                  f"party / candidate / leadership catcodes")
            print(f"  RECEIVE  in scope            ${f.recv_in_scope:>15,.0f}  "
                  f"(candidate money only)")
            print(f"           attributed          ${f.recv_attributed:>15,.0f}  "
                  f"{pct(f.recv_attributed, f.recv_in_scope)}")
            print(f"           dropped: no seat    ${f.recv_drop_no_committee_seat:>15,.0f}  "
                  f"{pct(f.recv_drop_no_committee_seat, f.recv_in_scope)}   "
                  f"challengers / open seats")
            print(f"           (committee-to-committee money, not in receive scope: "
                  f"${f.recv_drop_not_to_candidate:,.0f})")

        print(f"\nBy {args.by} — floors ${MIN_DOLLARS:,} and {MIN_COUNTERPARTIES} "
              f"counterparties. Post-fan-out, so send may exceed the funnel above.\n")
        head = f"{'category':<34} {'SEND':<5} {'send $':>14} {'from':>6}"
        if recv_available:
            head += f"   {'RECV':<5} {'recv $':>13} {'from':>6}"
        print(head); print("-" * (len(head) + 2))
        for _, x in chunk.iterrows():
            line = (f"{str(x.category)[:34]:<34} {x['send']:<5} "
                    f"${x.send_amount:>13,.0f} {int(x.send_parties):>6}")
            if recv_available:
                line += (f"   {x['recv']:<5} ${x.recv_amount:>12,.0f} "
                         f"{int(x.recv_parties):>6}")
            print(line)
        ok_s = (chunk["send"] == "OK").sum()
        tail = (f" · {(chunk['recv'] == 'OK').sum()} of {len(chunk)} on receive"
                if recv_available else "")
        print(f"{'':<34} {ok_s} of {len(chunk)} publishable on send{tail}")

    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        cols = ["quarter", "category", "send", "send_rows", "send_amount", "send_parties"]
        if recv_available:
            cols += ["recv", "recv_rows", "recv_amount", "recv_parties"]
        out = tbl[tbl["quarter"].isin(quarters)][cols].merge(
            fun.reset_index().rename(columns={"index": "quarter"}), on="quarter", how="left")
        out.to_csv(args.csv, index=False)
        print(f"\nwrote {args.csv}  (category rows + per-quarter funnel columns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
