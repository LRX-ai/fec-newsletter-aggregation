#!/usr/bin/env python3
"""Generate the last-column explanations for every newsletter table.

    python tools/build_descriptions.py --channel "Finance & Insurance" --quarter 2026Q2

Writes cache/pac_descriptions.json, keyed so a committee explained in one edition
is free in the next. Run with --show to print what it produced for review; the
`uses_outside_knowledge` flag marks any sentence that leans on something the FEC
filings do not state, which is exactly the set a human should read before publish.
"""
from __future__ import annotations
import argparse, json, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
import pandas as pd
from periods import period_mask, period_label
from fec_newsletter import common, config, congress, describe, extract, pipelines, sources


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
                         max_tied: int = MAX_TIED_SHOWN,
                         already_named: bool = False) -> tuple[str, int]:
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
    # A caller that has already formatted a person's name passes already_named, so
    # prettify does not re-title-case "(R-TX)" into "(R-Tx)".
    label = lambda cid, n: ((str(n) if already_named else prettify(n))
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


NAME_DROP = {"JR", "SR", "II", "III", "IV", "V", "MR", "MRS", "MS", "DR",
             "SEN", "REP", "HON", "SENATOR", "CONGRESSMAN", "CONGRESSWOMAN"}


def _person(name: str) -> str:
    """FEC files a name as "PLATNER, GRAHAM"; a newspaper prints "Graham Platner"."""
    name = re.sub(r'"[^"]*"', " ", str(name))
    if "," in name:
        last, first = name.split(",", 1)
        parts = [w for w in first.replace(".", " ").split() if w.upper() not in NAME_DROP]
        name = " ".join(parts + [last.strip()])
    return " ".join(w for w in name.title().split() if w.upper() not in NAME_DROP)


def _seat(cand) -> str:
    """A Senate race is statewide. Its district field is "00", which reads to a model
    as a zeroth district and comes back as "Maine's 0th District"."""
    if str(cand["CAND_OFFICE"]) == "S":
        return f'{cand["CAND_OFFICE_ST"]} (Senate)'
    d = str(cand["CAND_OFFICE_DISTRICT"] or "").strip()
    return f'{cand["CAND_OFFICE_ST"]}-{d.zfill(2)}' if d and d != "nan" else str(cand["CAND_OFFICE_ST"])


def _all_transactions(data, period):
    """Every deduped transaction for one period, keyed to the candidate it names.

    The receive pipeline cannot be reused here: it drops exactly the candidates this
    section exists to cover, because they hold no committee seat.
    """
    cov = common.Coverage()
    t = sources.dedupe(pd.concat([sources.from_oth(data["oth"], cov),
                                  sources.from_pas2(data["pas2"], cov)], ignore_index=True), cov)
    t = common.add_quarter(t, cov, config.SEND_QUARTER_START, config.SEND_QUARTER_END)
    t["cand"] = t["recip_cand_id"].fillna(t["recip_id"])
    return t[period_mask(t, period)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", default="Finance & Insurance")
    ap.add_argument("--period", "--quarter", dest="period", default=config.DEFAULT_PERIOD,
                    help=f"a month (2026-06) or a quarter (2026Q2); "
                         f"default {config.DEFAULT_PERIOD}")
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

    s = send[(send["newsletter"] == args.channel) & period_mask(send, args.period)]
    r = recv[(recv["newsletter"] == args.channel) & period_mask(recv, args.period)]
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
        beh = {"period_this_edition_covers": period_label(args.period),
               "recipients_this_period": int(sub["counterparty_name"].nunique()),
               "share_to_largest_recipient": f"{g.iloc[0] / tot * 100:.0f}%",
               "flow_types": sorted(sub["flow_type"].unique())}
        if sub["counterparty_name"].nunique() <= 5:
            beh["every_recipient_this_period"] = [str(k) for k in g.index]
        items.append({"key": f"send|{args.period}|{sender}|{g.index[0]}",
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
        items.append({"key": f"recv|{args.period}|{g.index[0]}|{member}", "facts": facts})

    # ---- RECEIVE section B: the races those seats sit in -------------------------
    # Contenders sit on no committee, so the receive pipeline drops them and their money
    # is reported only in this section. Derived from the seats of the members ranked
    # above rather than transcribed, so next quarter's races regenerate with everything
    # else -- the previous set of race sentences had no generator at all and silently
    # survived a prompt change.
    races = pd.read_csv(config.CACHE_DIR / "candidate_races.csv", dtype=str)
    races = races.sort_values("CAND_ELECTION_YR").drop_duplicates("CAND_ID", keep="last")
    fecmap = congress.fec_id_map(data["legislators"])
    txns = _all_transactions(data, args.period)
    cmte_name = cm.drop_duplicates("CMTE_ID").set_index("CMTE_ID")["CMTE_NM"]

    ranked = clean.groupby("attributed_label")["amount"].sum().sort_values(ascending=False).head(5)
    seen = set()
    for member in ranked.index:
        bg = clean.loc[clean["attributed_label"] == member, "bioguide"].iloc[0]
        mine = set(fecmap.loc[fecmap["bioguide"] == bg, "cand_id"])
        seat_rows = races[races["CAND_ID"].isin(mine)]
        if seat_rows.empty:
            continue
        seat = seat_rows.iloc[0]
        contenders = races[(races["CAND_OFFICE"] == seat["CAND_OFFICE"])
                           & (races["CAND_OFFICE_ST"] == seat["CAND_OFFICE_ST"])
                           & (races["CAND_OFFICE_DISTRICT"].fillna("") == (seat["CAND_OFFICE_DISTRICT"] or ""))
                           & (~races["CAND_ID"].isin(mine))]
        for _, cand in contenders.iterrows():
            sub = txns[txns["cand"] == cand["CAND_ID"]]
            if sub.empty or cand["CAND_ID"] in seen:
                continue
            seen.add(cand["CAND_ID"])
            sup = sub[sub["support_oppose"] == "support"].groupby("sender_id")["amount"].sum().sort_values(ascending=False)
            opp = sub[sub["support_oppose"] == "oppose"].groupby("sender_id")["amount"].sum().sort_values(ascending=False)
            if not len(sup) and not len(opp):
                continue
            repaid = sub.loc[sub["flow_type"] == "loan_repayment", "amount"].sum()
            who = _person(str(cand["CAND_NAME"]))
            facts = {"the_contender": {
                        "name": who,
                        "party": str(cand["CAND_PTY_AFFILIATION"] or "")[:3],
                        "seat": _seat(cand),
                        "fec_incumbency_flag": {"I": "incumbent", "C": "challenger",
                                                "O": "open seat"}.get(str(cand["CAND_ICI"]), "unknown"),
                        "campaign_repaid_this_much_to_the_candidate_himself":
                            f"${repaid:,.0f} (a loan the candidate had made to his own campaign)"
                            if repaid else "none"}}
            if len(sup):
                facts["largest_committee_SUPPORTING_them"] = dict(
                    describe.committee_facts(sup.index[0], cm, xw),
                    amount=f"${sup.iloc[0]:,.0f}", total_support=f"${sup.sum():,.0f}")
            if len(opp):
                facts["largest_committee_SPENDING_AGAINST_them"] = dict(
                    describe.committee_facts(opp.index[0], cm, xw),
                    amount=f"${opp.iloc[0]:,.0f}", total_opposition=f"${opp.sum():,.0f}")
            else:
                facts["opposition"] = "no committee spent against this candidate this quarter"
            items.append({"key": f"race|{args.period}|{_seat(cand)}|{who}",
                          "facts": facts})

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
