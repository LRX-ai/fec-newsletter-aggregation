#!/usr/bin/env python3
"""Generate the last-column explanations for every newsletter table.

    python tools/build_descriptions.py --channel "Finance & Insurance" --quarter 2026Q2

Writes cache/pac_descriptions.json, keyed so a committee explained in one edition
is free in the next. Run with --show to print what it produced for review; the
`uses_outside_knowledge` flag marks any sentence that leans on something the FEC
filings do not state, which is exactly the set a human should read before publish.
"""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pandas as pd
from fec_newsletter import config, congress, describe, extract, pipelines


MAX_TIED_SHOWN = 3   # list every tied top recipient up to this many, else just one

# FEC stores committee names in caps. Title-casing reads better in a table, but
# naive .title() mangles the acronyms these names are full of (PAC -> Pac).
ACRONYMS = {"PAC", "PACS", "AICPA", "RNC", "DNC", "DSCC", "DCCC", "NRSC", "NRCC",
            "USA", "US", "AFL", "CIO", "LLC", "INC", "BDA", "TAR", "AI", "ICT", "IT"}


def prettify(name: str) -> str:
    out = []
    for w in str(name).split():
        core = w.strip("().,;/&-")
        # Explicit list only. A length heuristic turns ordinary words into
        # acronyms -- "JOBS" is four uppercase letters and is not one.
        if core.upper() in ACRONYMS:
            out.append(w.upper())
        else:
            out.append(w.title())
    return " ".join(out)


def principal_recipients(sub: pd.DataFrame, self_id: str | None = None,
                         max_tied: int = MAX_TIED_SHOWN) -> tuple[str, int]:
    """(principal recipient label, distinct recipient count).

    Ties are common and not noise: FEC contribution limits push many recipients to
    exactly the same figure, so a quarter can have 39 committees level at $10,000.
    A tie of a few is meaningful and gets listed in full -- PricewaterhouseCoopers
    gave $30,000 each to the RNC and the DNC, which only reads as deliberate when
    both are shown. Beyond `max_tied` the tie is an artefact of the limit rather
    than a decision, so one name is shown and the rest are left to the count column.

    A committee's own ID is kept in the ranking and labelled: an internal transfer
    can be the single largest line, and silently dropping it would misreport where
    the money actually went.

    Each name carries its share of the sender's quarter. Without it "principal
    recipient" is ambiguous between a sender that gave one committee everything and
    one that spread across 265 and merely gave this one slightly the most -- the
    share is the whole difference between those two rows.
    """
    g = sub.groupby(["counterparty_id", "counterparty_name"])["amount"].sum()
    g = g.sort_values(ascending=False)
    if g.empty:
        return "—", 0
    total = g.sum()
    top = g.iloc[0]
    tied = [(cid, name) for (cid, name), v in g.items() if v == top]
    pct = 100 * top / total if total else 0
    share = "<1%" if 0 < pct < 1 else f"{pct:.0f}%"
    label = lambda cid, n: (prettify(n)
                            + (" (itself)" if self_id and cid == self_id else "")
                            + f" {share}")
    if len(tied) <= max_tied:
        return " · ".join(label(c, n) for c, n in sorted(tied, key=lambda t: t[1])), len(g)
    c, n = sorted(tied, key=lambda t: t[1])[0]
    return f"{label(c, n)} (one of {len(tied)} tied)", len(g)


def principal_counterparty(sub: pd.DataFrame, self_id: str | None = None):
    """Largest counterparty, EXCLUDING the entity itself.

    A diffuse giver like Realtors PAC has its own committee as the single largest
    line (an internal transfer), which makes "principal recipient" meaningless and
    produces a row that reads as a data error.
    """
    g = sub.groupby("counterparty_name")["amount"].sum().sort_values(ascending=False)
    if self_id is not None:
        own = set(sub.loc[sub["counterparty_id"] == self_id, "counterparty_name"])
        g = g.drop(labels=[n for n in own if n in g.index], errors="ignore")
    return g


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="Finance & Insurance")
    ap.add_argument("--quarter", default="2026Q2")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="build facts, make no API call")
    args = ap.parse_args()

    data = extract.load({n: config.CACHE_DIR / c for n, (_, c) in config.EXTRACTS.items()})
    cm, xw = data["committees"], data["crosswalk"]
    send, _ = pipelines.run_send(data)
    recv, _ = pipelines.run_receive(data)
    memb = congress.current_memberships(data["membership"])
    sub_map = congress.load_mapping()
    sub_map = sub_map[sub_map["subcommittee_thomas_id"].str.strip() != ""].copy()
    sub_map["code"] = sub_map["thomas_id"] + sub_map["subcommittee_thomas_id"].str.strip().str.zfill(2)
    subname = sub_map.set_index("code")["subcommittee_name"]
    cn = congress.committee_newsletters()
    chan_codes = set(cn[cn["newsletter"] == args.channel]["code"])

    s = send[(send["newsletter"] == args.channel) & (send["quarter"].astype(str) == args.quarter)]
    r = recv[(recv["newsletter"] == args.channel) & (recv["quarter"].astype(str) == args.quarter)]
    clean = r[(r["support_oppose"] == "support") & (~r["flow_type"].isin(["loan", "loan_repayment"]))]
    name2id = cm.drop_duplicates("CMTE_NM").set_index("CMTE_NM")["CMTE_ID"]
    items = []

    # ---- SEND section A: channel PAC -> its principal recipient ------------------
    for sender, tot in s.groupby("attributed_name")["amount"].sum().sort_values(ascending=False).head(6).items():
        sub = s[s["attributed_name"] == sender]
        sid = sub["sender_id"].iloc[0]
        g = principal_counterparty(sub, sid)
        if g.empty:
            continue
        rid = name2id.get(g.index[0], sub.loc[sub["counterparty_name"] == g.index[0], "counterparty_id"].iloc[0])
        beh = {"recipients_this_quarter": int(sub["counterparty_name"].nunique()),
               "share_to_largest_recipient": f"{g.iloc[0] / tot * 100:.0f}%",
               "flow_types": sorted(sub["flow_type"].unique())}
        if sub["counterparty_name"].nunique() <= 5:
            beh["every_recipient_this_quarter"] = [str(k) for k in g.index]
        items.append({"key": f"send|{sender}|{g.index[0]}",
                      "facts": describe.pair_facts(sid, rid, cm, xw, beh)})

    # ---- RECEIVE section A: principal backer -> member ---------------------------
    for member, tot in clean.groupby("attributed_label")["amount"].sum().sort_values(ascending=False).head(5).items():
        sub = clean[clean["attributed_label"] == member]
        g = sub.groupby("counterparty_name")["amount"].sum().sort_values(ascending=False)
        bid = sub.loc[sub["counterparty_name"] == g.index[0], "counterparty_id"].iloc[0]
        bg = sub["bioguide"].iloc[0]
        seats = sorted(set(memb.loc[memb["bioguide"] == bg, "committee"]) & chan_codes)
        seat = next((subname[c] for c in seats if len(c) == 6 and c in subname.index), None)
        # This backer's OWN stance, computed per backer. Previously the member's
        # opposition total sat next to the backer's stats and the model read it as the
        # backer's behaviour, producing three confidently wrong sentences.
        stance = sorted(sub_rows := set(
            clean[(clean["attributed_label"] == member)
                  & (clean["counterparty_name"] == g.index[0])]["support_oppose"]))
        backer_all = r[(r["attributed_label"] == member) & (r["counterparty_name"] == g.index[0])]
        backer_stance = sorted(set(backer_all["support_oppose"]))
        opponents = (r[(r["attributed_label"] == member) & (r["support_oppose"] == "oppose")]
                     .groupby("counterparty_name")["amount"].sum().sort_values(ascending=False))
        facts = {"backer": describe.committee_facts(bid, cm, xw),
                 "recipient_is_a_sitting_member_of_congress": {
                     "name_party_seat": member,
                     "subcommittee_they_sit_on": seat or "committee seat",
                     "newsletter_channel_this_edition_covers": args.channel},
                 "what_THIS_BACKER_did": {
                     "stance": " and ".join(backer_stance) or "support",
                     # Direction matters and the model inverted this twice when the key
                     # was shorter: it is the member's backing that is concentrated, not
                     # the backer's spending.
                     "pct_of_THIS_MEMBERS_total_backing_that_came_from_this_backer":
                         f"{g.iloc[0] / tot * 100:.0f}%",
                     "note": ("this does NOT say what share of the backer's own spending "
                              "went to this member, which is unknown here")},
                 "about_the_member_not_this_backer": {
                     "total_backers_supporting_them": int(sub["counterparty_name"].nunique()),
                     "opposed_by_OTHER_committees": (
                         f"{opponents.index[0]} (${opponents.iloc[0]:,.0f})"
                         if len(opponents) else "no opposition spending")}}
        aff = describe.affiliates(bid, cm)
        if aff:
            facts["backer"]["affiliated_committees_per_filings"] = aff
        items.append({"key": f"recv|{g.index[0]}|{member}", "facts": facts})

    # Recipient breakdowns for the send table, computed rather than transcribed.
    breakdowns = {}
    for sender, tot in s.groupby("attributed_name")["amount"].sum().sort_values(ascending=False).head(6).items():
        sub = s[s["attributed_name"] == sender]
        principal, count = principal_recipients(sub, sub["sender_id"].iloc[0])
        breakdowns[sender] = {"principal": principal, "recipients": count}
    bpath = config.REPO_ROOT / "cache" / "recipient_breakdowns.json"
    bpath.write_text(json.dumps(breakdowns, indent=1))
    print(f"recipient breakdowns -> {bpath.name}")
    for k, v in breakdowns.items():
        print(f"   {k[:38]:<38} {v['recipients']:>4} recipients   {v['principal']}")

    print(f"\n{len(items)} pairs")
    if args.dry_run:
        print(json.dumps(items[:2], indent=1, default=str))
        return 0

    out = describe.describe(items)
    if args.show:
        for it in items:
            d = out.get(it["key"], {})
            flag = "   [outside knowledge]" if d.get("uses_outside_knowledge") else ""
            k = it["key"].split("|")
            print(f"\n{k[0].upper()}  {k[1][:38]} -> {k[2][:34]}{flag}")
            print(f"   {d.get('sentence', '(none)')}")
    flagged = sum(1 for d in out.values() if d.get("uses_outside_knowledge"))
    print(f"\n{len(out)} described; {flagged} rely on outside knowledge and need review")
    print(f"cached in {describe.CACHE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
