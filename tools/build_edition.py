#!/usr/bin/env python3
"""Generate the send and receive editions for ANY channel, computed rather than typed.

The first pair of editions had every figure written into the source by hand. That was
fine for one channel and is not a way to run nineteen: a crosswalk edit moved thirty
numbers across two files and each had to be found and replaced. Everything here is
derived from the pipelines at build time, so a mapping change is one rerun.

    python tools/build_edition.py --channel Defense
    python tools/build_edition.py --channel "Finance & Insurance"

Reads the dashboard JSON that build_recipient_table / build_committee_roster /
build_challenger_pairs emit for the same channel, and the sentence cache that
build_descriptions writes. Run those first.
"""
from __future__ import annotations
import argparse, datetime as dt, json, re, sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src")); sys.path.insert(0, str(REPO / "tools"))
import pandas as pd
from fec_newsletter import common, config, congress, elections, extract, pipelines, sources
import edition_ui as ui
from edition_ui import (money, bars, vstack, vgroup, vtrend, routes, figure, note, doc,
                        slug, short_cmte, period_mask, is_month, period_label)
from build_descriptions import prettify, principal_recipients

AS_OF = dt.date(2026, 8, 28)
NAME_DROP = {"JR","SR","II","III","IV","V","MR","MRS","MS","DR","SEN","REP","HON"}


def person(name):
    name = re.sub(r'"[^"]*"', " ", str(name))
    if "," in name:
        last, first = name.split(",", 1)
        parts = [w for w in first.replace(".", " ").split() if w.upper() not in NAME_DROP]
        name = " ".join(parts + [last.strip()])
    return " ".join(w for w in name.title().split() if w.upper() not in NAME_DROP)


def load_desc():
    """(sentence by key, sources by key). Sources are the pages the writer consulted."""
    p = config.REPO_ROOT / "cache" / "pac_descriptions.json"
    d = json.loads(p.read_text()) if p.exists() else {}
    return ({k: v.get("sentence", "") for k, v in d.items() if v.get("sentence")},
            {k: [u for u in (v.get("sources") or []) if u] for k, v in d.items()})


def seat_label(row):
    st = str(row["CAND_OFFICE_ST"])
    if str(row["CAND_OFFICE"]) == "S":
        return f"{st}-Sen"
    d = str(row["CAND_OFFICE_DISTRICT"] or "").strip()
    return f"{st}-{d.zfill(2)}" if d and d.lower() != "nan" else st


def pgi_bucket(pgi, cid, meta, cal):
    p = str(pgi or "").strip().upper()
    st = elections.status(p, *meta.get(cid, ("", "", "")), as_of=AS_OF, cal=cal)
    yr = p[1:] if p[1:].isdigit() else ""
    if yr and int(yr) > 2026:
        return "2028 and 2030 cycles"
    if p.startswith("G") and yr == "2026":
        return "General election, 3 Nov 2026"
    if st == "ahead":
        return "Nomination contests still to come"
    if st == "settled":
        return "Nomination contests already held"
    return "Earlier cycles and undated"


def build(channel, period):
    data = extract.load({n: config.CACHE_DIR / c for n, (_, c) in config.EXTRACTS.items()})
    send, _ = pipelines.run_send(data)
    recv, _ = pipelines.run_receive(data)
    D, DSRC = load_desc()
    # Each edition cites only the pages behind the sentences IT prints, in the order they
    # appear, so the send edition does not carry the receive edition's reading list.
    used = {"send": [], "recv": []}

    def desc(key, side):
        used[side].append(key)
        return D.get(key, "")
    cm = data["committees"].drop_duplicates("CMTE_ID").set_index("CMTE_ID")["CMTE_NM"]
    races = pd.read_csv(config.CACHE_DIR / "candidate_races.csv", dtype=str).fillna("")
    races = races.sort_values("CAND_ELECTION_YR").drop_duplicates("CAND_ID", keep="last")
    ridx = races.set_index("CAND_ID")
    cal = elections.load()

    s_all = send[send["newsletter"] == channel].copy()
    r_all = recv[recv["newsletter"] == channel].copy()
    s = s_all[period_mask(s_all, period)].copy()
    r = r_all[period_mask(r_all, period)].copy()
    for df in (s_all, r_all, s, r):
        df["mo"] = pd.to_datetime(df["transaction_dt"]).dt.to_period("M")
    # A counterparty that is a CANDIDATE is a person, and the FEC files people as
    # "GONZALEZ, VICENTE MR.". Relabelled once here so every table that names a
    # counterparty reads as a name rather than as a filing key.
    def cand_label(cid, fallback):
        # A candidate outside the 2026 race file (a 2028 filer, say) still has a name in
        # the transaction, and it is still filed surname-first.
        if cid not in ridx.index:
            return person(fallback)
        row = ridx.loc[cid]
        who = person(row["CAND_NAME"])
        pty = str(row["CAND_PTY_AFFILIATION"])[:1]
        return f"{who} ({pty}-{seat_label(row)})" if pty in "DRIL" else who

    # Every counterparty is relabelled for display in one pass -- people as names,
    # committees through the same shortener Section A uses -- so the tables downstream
    # can print what they are given instead of each re-formatting differently.
    is_person = s["counterparty_id"].astype(str).str[:1].isin(list("HSP"))
    s["counterparty_name"] = [
        cand_label(c, n) if p else short_cmte(n, 46)
        for c, n, p in zip(s["counterparty_id"], s["counterparty_name"], is_person)]

    clean = r[(r["support_oppose"] == "support")
              & (~r["flow_type"].isin(["loan", "loan_repayment"]))]

    F = {"channel": channel, "period": period,
         "send_total": s["amount"].sum(), "senders": s["attributed_name"].nunique(),
         "recv_total": r["amount"].sum(), "recv_support": clean["amount"].sum(),
         "members": r["cand_id"].nunique()}

    # ---- send: attached vs unattached -------------------------------------------
    is_cand = s["counterparty_id"].astype(str).str[:1].isin(list("HSP"))
    att, un = s[is_cand], s[~is_cand]
    F["attached"], F["unattached"] = att["amount"].sum(), un["amount"].sum()
    F["cands_paid"] = att["counterparty_id"].nunique()

    def sender_rows(frame, n=6):
        out = []
        for name, tot in frame.groupby("attributed_name")["amount"].sum().nlargest(n).items():
            sub = frame[frame["attributed_name"] == name]
            sid = sub["sender_id"].iloc[0]
            lab, cnt = principal_recipients(sub, sid, already_named=True)
            g = sub.groupby("counterparty_name")["amount"].sum().sort_values(ascending=False)
            own = set(sub.loc[sub["counterparty_id"] == sid, "counterparty_name"])
            g = g.drop(labels=[x for x in own if x in g.index], errors="ignore")
            key = f"send|{period}|{name}|{g.index[0]}" if len(g) else ""
            out.append((short_cmte(name), round(tot), cnt, lab, desc(key, "send")))
        return out

    F["send_top"] = sender_rows(s)
    F["unattached_top"] = sender_rows(un, 6)

    # race-attached senders, read the other way
    ra = []
    for name, tot in att.groupby("attributed_name")["amount"].sum().nlargest(6).items():
        sub = att[att["attributed_name"] == name].copy()
        sub["lab"] = [
            (lambda x: f'{person(x["CAND_NAME"])} ({str(x["CAND_PTY_AFFILIATION"])[:1]}-{seat_label(x)})'
             if c in ridx.index and str(x["CAND_PTY_AFFILIATION"])[:1] in "DRIL" else person(x["CAND_NAME"])
             )(ridx.loc[c]) if c in ridx.index else str(c)
            for c in sub["counterparty_id"]]
        g = sub.groupby("lab")["amount"].sum().sort_values(ascending=False)
        top = g.iloc[0]; tied = sorted(g[g == top].index)
        pct = 100 * top / tot
        sh = "<1%" if 0 < pct < 1 else f"{pct:.0f}%"
        lab = (" · ".join(f"{t} {sh}" for t in tied) if len(tied) <= 3
               else f"{tied[0]} {sh} (one of {len(tied)} tied)")
        ra.append((short_cmte(name), round(tot), int(g.size), lab))
    F["race_attached_top"] = ra

    # ---- monthly designation split (send) ---------------------------------------
    def sbucket(p):
        p = str(p or "").strip().upper()
        return {"P2026": 0, "G2026": 1, "P": 2}.get(p, 3)
    s["sb"] = s["transaction_pgi"].map(sbucket)
    F["send_monthly"] = [(mo.strftime("%b %Y"),
                          [round(s[(s["mo"] == mo) & (s["sb"] == k)]["amount"].sum()) for k in range(4)])
                         for mo in sorted(s["mo"].unique())]
    # A single month has nothing to compare within itself, so the by-month split becomes
    # a plain ranking of the designations. The twelve-month trend still applies -- that
    # compares the period to its own history, which is the point of a monthly edition.
    F["send_designation"] = [(n, round(s[s["sb"] == k]["amount"].sum())) for k, n in
                             ((0, "Primary 2026"), (1, "General 2026"),
                              (2, "Undated (no election named)"),
                              (3, "Runoff, special, other cycles"))
                             if s[s["sb"] == k]["amount"].sum() > 0]

    def rolling(frame):
        m = frame.groupby("mo")["amount"].sum().sort_index()
        roll = m.rolling(12).mean()
        return [(k.strftime("%b %Y"), round(m[k]),
                 round(roll[k]) if pd.notna(roll.get(k)) else None) for k in m.index[-12:]]
    F["send_roll"], F["recv_roll"] = rolling(s_all), rolling(r_all)
    F["send_flow"] = [(k.replace("_", " ").capitalize(), round(v)) for k, v in
                      s.groupby("flow_type")["amount"].sum().sort_values(ascending=False).items()]

    # ---- receive: members, races, buckets ---------------------------------------
    # Ranked by money WEIGHTED by how much the member matters to this channel, not by
    # money alone. Dollars answer who collected the most; they do not answer whether the
    # collector is anywhere near the policy. A member on four of the channel's committees
    # including its first-ranked one is a different story from one holding a single
    # peripheral seat, and at equal dollars the first is the one worth naming.
    per = clean.groupby(["attributed_label", "bioguide"], as_index=False)["amount"].sum()
    imp = congress.member_importance(data["membership"], channel).set_index("bioguide")
    per = per.join(imp, on="bioguide")
    # No ranked seat means every committee they sit on was judged outside this channel,
    # so they are not a story about it -- they stay in the channel total and in Section D.
    per = per[per["importance"].fillna(0) > 0]
    per["score"] = per["amount"] * per["importance"]
    top = per.nlargest(5, "score").set_index("attributed_label")
    F["recv_weighted"] = len(per)
    memb_rows, seats_seen = [], []
    for m, row in top.iterrows():
        v = row["amount"]
        sub = clean[clean["attributed_label"] == m]
        g = sub.groupby("counterparty_name")["amount"].sum().sort_values(ascending=False)
        opp = r[(r["attributed_label"] == m) & (r["support_oppose"] == "oppose")]["amount"].sum()
        key = f"recv|{period}|{g.index[0]}|{m}"
        memb_rows.append((m,
                          round(v),
                          (prettify(g.index[0]), f"{100*g.iloc[0]/v:.0f}% of {g.size} backers"),
                          desc(key, "recv")))
        seats_seen.append((m, sub["cand_id"].iloc[0], round(opp)))
    F["recv_top"] = memb_rows
    F["opp_by_member"] = {m: o for m, _, o in seats_seen}

    meta = {c: (ridx.loc[c, "CAND_OFFICE_ST"], ridx.loc[c, "CAND_OFFICE"],
                ridx.loc[c, "CAND_OFFICE_DISTRICT"]) if c in ridx.index else ("", "", "")
            for c in r["cand_id"].unique()}
    r["b"] = [pgi_bucket(p, c, meta, cal) for p, c in zip(r["transaction_pgi"], r["cand_id"])]
    order = ["Nomination contests already held", "General election, 3 Nov 2026",
             "Earlier cycles and undated", "2028 and 2030 cycles",
             "Nomination contests still to come"]
    g = r.groupby("b")["amount"].sum()
    F["buckets"] = [(k, round(g.get(k, 0))) for k in order if g.get(k, 0)]
    F["forward"] = round(sum(v for k, v in F["buckets"]
                             if k not in ("Nomination contests already held",
                                          "Earlier cycles and undated")))
    nom = r[r["b"].str.startswith("Nomination")].groupby("mo")["amount"].sum()
    gen = r[r["b"].str.startswith("General")].groupby("mo")["amount"].sum()
    F["crossover"] = [(mo.strftime("%b %Y"), [round(nom.get(mo, 0)), round(gen.get(mo, 0))])
                      for mo in sorted(r["mo"].unique())]

    # ---- races contesting the five ranked seats ---------------------------------
    cov = common.Coverage()
    t = sources.dedupe(pd.concat([sources.from_oth(data["oth"], cov),
                                  sources.from_pas2(data["pas2"], cov)], ignore_index=True), cov)
    t = common.add_quarter(t, cov, config.SEND_QUARTER_START, config.SEND_QUARTER_END)
    t["cand"] = t["recip_cand_id"].fillna(t["recip_id"])
    tq = t[period_mask(t, period)]
    race_rows, members = [], set(r["cand_id"])
    for label, cid, _ in seats_seen:
        if cid not in ridx.index:
            continue
        inc = ridx.loc[cid]
        cont = races[(races["CAND_OFFICE"] == inc["CAND_OFFICE"])
                     & (races["CAND_OFFICE_ST"] == inc["CAND_OFFICE_ST"])
                     & (races["CAND_OFFICE_DISTRICT"] == inc["CAND_OFFICE_DISTRICT"])
                     & (races["CAND_ID"] != cid) & (~races["CAND_ID"].isin(members))]
        for _, c in cont.iterrows():
            sub = tq[tq["cand"] == c["CAND_ID"]]
            sup = sub[sub["support_oppose"] == "support"]["amount"].sum()
            opp = sub[sub["support_oppose"] == "oppose"]
            if sup < 25000 and opp["amount"].sum() < 25000:
                continue
            og = opp.groupby("sender_id")["amount"].sum().sort_values(ascending=False)
            oppcell = ((f'${og.sum():,.0f} — {prettify(str(cm.get(og.index[0], og.index[0])))}',
                        f"{og.sum()/sup:.0f}x" if sup else "—") if len(og) else ("none", "—"))
            who = person(c["CAND_NAME"])
            st = {"I": "incumbent", "C": "challenger", "O": "open seat"}.get(str(c["CAND_ICI"]), "")
            key = f"race|{period}|{seat_label(c)}|{who}"
            race_rows.append(((seat_label(inc), label.split("(")[0].strip()),
                              (f'{who} ({str(c["CAND_PTY_AFFILIATION"])[:1]})', st),
                              round(sup), oppcell, desc(key, "recv")))
    race_rows.sort(key=lambda x: -x[2])
    F["races"] = race_rows[:6]

    def _sources(side):
        seen, out = set(), []
        for k in used[side]:
            for u in DSRC.get(k, []):
                if u not in seen:
                    seen.add(u)
                    out.append(u)
        return out

    F["sources_send"], F["sources_recv"] = _sources("send"), _sources("recv")

    # ---- coverage & redundancy ---------------------------------------------------
    pas2 = data["pas2"].copy()
    pas2["amt"] = pd.to_numeric(pas2["TRANSACTION_AMT"], errors="coerce")
    d = pd.to_datetime(pas2["TRANSACTION_DT"], format="%m%d%Y", errors="coerce")
    pas2 = pas2.assign(transaction_dt=d)
    F["all_cand_money"] = round(pas2[period_mask(pas2, period)]["amt"].sum())
    shared = set(r["sub_id"]) & set(s["sub_id"])
    F["shared"] = len(shared)
    F["shared_r"] = round(100 * len(shared) / max(len(r), 1))
    F["shared_s"] = round(100 * len(shared) / max(len(s), 1))
    lr = r[r["flow_type"] == "loan_repayment"].groupby("attributed_label")["amount"].sum()
    F["loan_top"] = (lr.idxmax(), round(lr.max()),
                     round(r[r["attributed_label"] == lr.idxmax()]["amount"].sum())) if len(lr) else None
    return F


def render(F, ui_send, ui_recv):
    """Both editions as HTML. Every figure comes from F; nothing is typed twice."""
    Q, CH = F["period"], F["channel"]
    QL = period_label(Q)
    MONTHLY = is_month(Q)
    # "this quarter" on a monthly edition reads as a copy-paste error, so the noun is
    # carried in a variable rather than written into thirty sentences.
    PER = "month" if MONTHLY else "quarter"
    pctf = lambda v, t: f"{round(100 * v / t)}%" if t else "0%"
    RD = ui_send["dash"] if ui_send else {}
    CD = ui_recv["roster"] if ui_recv else {}
    HD = ui_recv["pairs"] if ui_recv else {}

    # One month has no internal handover to draw, so the block is omitted rather than
    # rendered as a single-group chart that implies a comparison it cannot make.
    # One month has no month-over-month split to stack, so the designation mix becomes a
    # plain ranking. Stacking a single column would render one full-width bar and imply
    # a comparison against nothing.
    DESIGNATION = (
        figure("Contributions by election designation", bars(F["send_designation"], ui.PRIMARY))
        if MONTHLY else
        figure("Monthly contributions by election designation", vstack(
            F["send_monthly"],
            ["Primary 2026", "General 2026", "Undated (no election named)",
             "Runoff, special, other cycles"],
            [ui.PRIMARY, ui.GENERAL, ui.UNDATED, ui.OTHER],
            (F["send_monthly"][-1][0],), normalize=True)))

    CROSSOVER = "" if MONTHLY else (
        "<h2>The handover inside this quarter</h2>\n"
        + figure("Nomination money against general-election money, by month",
                 vgroup(F["crossover"], ["Nomination", "General"],
                        [ui.PRIMARY, ui.GENERAL],
                        share_label="the general share of the month")))

    SEND = RECV = ""
    top1 = F["send_top"][0] if F["send_top"] else ("", 0, 0, "", "")
    send_share = pctf(top1[1], F["send_total"])
    if ui_send:
     SEND = doc(f"""
<p>Each {PER} we track movements in US federal campaign finance, channel by channel. This
edition covers <strong>{CH}</strong> for {QL}, ranking the channel's own political action
committees by what they gave, and showing who each of them backed.</p>

<div class="key-takeaways"><h2>Key Takeaways</h2>
<div class="ctx-item"><h3>The channel moved {money(F['send_total'])} across {F['senders']} committees</h3>
<p>Its largest giver, {top1[0]}, accounts for {money(top1[1])} &mdash; {send_share} of the {PER}.
The money reached {F['cands_paid']} candidates directly and a further {money(F['unattached'])} moved
committee to committee without naming one.</p></div>
<div class="ctx-item"><h3>{pctf(F['attached'], F['send_total'])} of it reached a candidate</h3>
<p>{money(F['attached'])} went to candidates and {money(F['unattached'])} did not, moving between
committees before the races it will be spent on were chosen. Section C is about that second
part.</p></div>
<div class="ctx-item"><h3>General-election money is the part that is still live</h3>
<p>Money designated for the 3 November general is committed to a race that has not been held;
money designated for a nomination contest mostly is not. Section B separates them.</p></div></div>

<h1>Section A: Largest Senders</h1>
<p>This section ranks the {CH} committees that gave the most in {QL}, alongside the recipient each
one principally backed. A channel edition ranks the channel's own side: here that is the PACs doing
the sending.</p>

{figure(f"Largest senders in the channel, {QL}", routes(
  ["Sender", "Sent", "Recipients", "Principal recipient", "What this is"], F["send_top"]))}

<h1>Section B: Contribution Trend</h1>
<p>The channel's disclosed contributions in {QL}, split by the election each was designated for. Money designated for <em>no</em> named election is kept separate rather than
counted as primary giving.</p>

{DESIGNATION}

<h2>{QL} against the last twelve months</h2>
<p>A single {PER} cannot distinguish a heavy one from a channel that has simply grown. The tick
on each column is that month's trailing twelve-month average, so the comparison is against the channel's own moving norm rather than against the one next door.</p>

{figure("Monthly totals against the trailing 12-month average", vtrend(F["send_roll"]))}

<h1>Section C: Money Not Yet Attached to a Race</h1>
<p><strong>{money(F['unattached'])} of {money(F['send_total'])} &mdash;
{pctf(F['unattached'], F['send_total'])} &mdash; moved committee to committee</strong>, with no
candidate on the receiving end and no race it can be assigned to. That is not a gap in the data: it
is money being positioned before the races it will be spent on are chosen.</p>

{figure(f"Committee-to-committee money, {QL}", routes(
  ["Sender", "Sent", "Destinations", "Principal destination", "What this is"], F["unattached_top"]))}

<h2>Where the attached share is pointed</h2>
<p>The {money(F['attached'])} that did reach candidates went to {F['cands_paid']} of them. The same
senders are shown here the other way round: who they backed, rather than which committee they
funded.</p>

{figure(f"Race-attached money, {QL}", routes(
  ["Sender", "Sent", "Candidates", "Principal candidate"], F["race_attached_top"]))}

<h1>Section D: Every Politician the Channel Paid in {QL}</h1>
<p>Section A ranks the six committees that gave the most. That is a fair account of who <em>gave</em>
and a poor one of who <em>received</em>: in {QL} the same {money(F['attached'])} reached
<strong>{RD['candidates']} candidates</strong> from {RD['senders']} committees. Here is all of it
&mdash; sortable by any column, and searchable by name, seat or PAC.</p>
<p>Like every other ranking in this edition, the table is <strong>{QL} only</strong>. A figure here
is what that politician received in that period, not a running total.</p>

<div class="figure"><p class="figcap">Every recipient of {CH} money, {QL}</p>
{ui_send['dash_html']}</div>

<h1>Section E: How the Money Moved</h1>
<p>This section separates {QL} by transaction type. These are different kinds of money and are
never summed together: a direct contribution reaches the recipient's account, while an independent
expenditure is spent about a candidate who never receives it and cannot coordinate on it.</p>

{figure(f"{QL} by flow type", bars(F["send_flow"], ui.PRIMARY))}

<h2>Methodology</h2>
{METHOD_SEND.format(CH=CH, QL=QL, AS_OF=ui.AS_OF)}

{ui.sources_list(F["sources_send"])}
""")

    fwd_pct = pctf(F["forward"], F["recv_total"])
    if ui_recv:
     RECV = doc(f"""
<p>Each {PER} we track movements in US federal campaign finance, channel by channel. This edition
covers <strong>{CH}</strong> for {QL}, ranking the members of Congress on the channel's committee
seats by the money backing them, and showing who is funding the candidates trying to take those
seats.</p>

<div class="context"><h2>Central Themes in this {PER.title()}&rsquo;s Contribution Data</h2>
<div class="ctx-item"><h3>{money(F['recv_support'])} backed members who write {CH.lower()} policy</h3>
<p>Of {money(F['recv_total'])} reaching these seats in total, {money(F['recv_support'])} was spent
supporting the member. The rest was spent against them, or was a campaign repaying a loan to its own
candidate &mdash; neither is industry backing, and both are separated out here.</p></div>
<div class="ctx-item"><h3>{fwd_pct} of the {PER} is aimed at races not yet run</h3>
<p>{money(F['forward'])} is designated for the 3 November general or for the 2028 and 2030 cycles,
or for a nomination contest that had not been held when this went out. That is the part of the
edition with predictive content.</p></div>
<div class="ctx-item"><h3>{F['members']} members hold a seat this channel covers</h3>
<p>Attribution runs through congressional committee membership, which is what ties a dollar to a
policy area rather than to a personality. Section D widens the frame to the candidates contesting
those same seats.</p></div></div>

<h1>Section A: Who Is Funding the Most Important Members?</h1>
<p>We rank the {F['recv_weighted']} members on {CH.lower()} seats by money spent <em>supporting</em> 
them in {QL} <strong>weighted by how much each member matters to this channel</strong> &mdash; how many
of its committees they sit on, how central those committees are, and how senior they are on each. A member
on committees that influence policy more in {CH.lower()} outranks one holding a peripheral seat with more
backing.</p>

{figure(f"Members ranked by backing weighted by committee standing, {QL}", routes(
  ["Member", "Backed by", "Principal backer", "What this is"], F["recv_top"]))}

<p>These are the same members set against the people contesting their seats, so the incumbent and the
race for their seat can be read together; Section D does this for every seat the channel covers.</p>

{figure(f"The same members against the money contesting their seats, {QL}", routes(
  ["Seat", "Contender", "Backed with", "Spent against them", "What this is"],
  [(a, b, c, d, e) for (a, b, c, d, e) in F["races"]]) if F["races"] else
  "<p>No contender in these seats moved enough money this quarter to report.</p>")}

<h1>Section B: What This Money Is Aimed At</h1>
<p>Every contribution names the election it is for, and the FEC publishes the date of every 2026
primary. Together those settle which money describes a decided race and which is a position on one
still to come: <strong>{money(F['forward'])} of {QL}'s {money(F['recv_total'])} &mdash;
{fwd_pct} &mdash; is aimed at elections that have not been held</strong>.</p>

{figure(f"Where the {QL} money is pointed, as at {ui.AS_OF}", bars(F["buckets"], ui.PRIMARY))}

{CROSSOVER}

<h2>The last twelve months</h2>
{figure("Monthly arrivals", vtrend(F["recv_roll"], average=False))}

<h1>Section C: Who Sits on These Committees, and Who Funds Them</h1>
<p>This is the full roster of the ten highest-ranking members of <strong>each of the
{CD['units']} committees and subcommittees that set {CH} policy</strong>, {CD['rows']} seats held by
{CD['members']} people, with what each collected in {QL} and their largest backer. Committees run in
order of how central they are to the channel, not alphabetically.</p>
<p>Collected is <strong>all</strong> support money reaching that member, not just this channel's
share: a channel-attributed slice of one person's period is a fraction of a fraction.</p>

<div class="figure"><p class="figcap">Senior members of every {CH} committee, {QL}</p>
{ui_recv['roster_html']}</div>

<h1>Section D: Incumbents Against the Money Behind Their Challengers</h1>
<p>Tracking money through committee seats is what makes it readable as {CH.lower()} policy, and it
names the people who hold those seats. This section puts the other side of every one of those races
beside them &mdash; the candidates contesting the same seats, in full:
<strong>{HD['pairs']} pairings across {HD['seats']} seats</strong>.</p>
<p>The channel accounts for {money(F['recv_total'])} reaching the members it covers. The seats those
members hold have a further <strong>{money(HD['ch_money'])} behind their challengers</strong> and
{money(HD['opp_money'])} spent against those challengers &mdash; the rest of the money in play for
the same seats.</p>

<div class="figure"><p class="figcap">{CH} incumbents and the money contesting their seats, {QL}</p>
{ui_recv['pairs_html']}</div>

<h2>Methodology</h2>
{METHOD_RECV.format(CH=CH, QL=QL, AS_OF=ui.AS_OF, ALL=money(F['all_cand_money']),
                    SHARED=f"{F['shared']:,}", SR=F['shared_r'], SS=F['shared_s'])}

{ui.sources_list(F["sources_recv"])}
""")
    return SEND, RECV


METHOD_COMMON = """<p><strong>Designation.</strong> Every row names the election it was given for
(FEC <code>TRANSACTION_PGI</code>), which is a statement of intent rather than a date we inferred.
Where a recipient is a candidate we resolve that against the FEC's published 2026 primary calendar
using their state, office and district, judged as at <strong>{AS_OF}</strong> &mdash; the date this edition is read, not the end of the period it covers. Alabama splits its districts between May and
August and Louisiana holds its House primaries on 3 November, so both need more than a state lookup.
Source: FEC, <em>2026 Congressional Primary Dates</em>, data as at 18 May 2026.</p>
<p><strong>None of this is filed after the fact.</strong> We check the report each row was disclosed
on (<code>RPT_TP</code>). No money in {QL} arrived on a post-general or post-primary report; the bulk came
on pre-primary reports, due twelve days <em>before</em> the vote, and the rest on routine periodic filings.</p>
<p><strong>How current this is.</strong> Disclosure lags the transaction by a median of 32 days, 63
at the 90th percentile, and a periodic edition adds more. Treat this as positioning intelligence
rather than a live feed.</p>"""

METHOD_SEND = """<p>Every figure starts from the <strong>transaction amount</strong> of each
contribution disclosed to the Federal Election Commission. We combine two FEC bulk files &mdash;
committee-to-committee transfers and committee-to-candidate contributions &mdash; and remove the
overlap, since the same transaction appears in both and both parties to a transfer file it
separately.</p>
<p><strong>How a channel is assigned.</strong> A sending committee carries a CRP industry code, and
that code maps to this channel. The scope is deliberately limited to senders we can classify: a
committee with no catcode contributes nothing to these totals rather than being guessed at.</p>
""" + METHOD_COMMON

METHOD_RECV = """<p>Every figure starts from the same FEC transaction amounts as the send edition,
restricted to money whose recipient is a <strong>candidate</strong>.</p>
<p><strong>How a channel is assigned.</strong> Candidates have no industry code, so a contribution
counts toward the channel of the <strong>congressional committees its recipient sits on</strong>. We
join the FEC candidate ID to the sitting member, then to their current committee and subcommittee
assignments, then to the channels those cover. Attribution is at subcommittee level where a member
holds a subcommittee seat, since a named subcommittee is a sharper signal than its parent.</p>
<p><strong>How the money is divided.</strong> A member typically sits on several committees, and a
committee may cover several channels. Rather than credit the full amount to each, we divide a
contribution <strong>evenly</strong> across the member's committees and evenly again across each
committee's channels, so channel totals add up without double-counting.</p>
<p><strong>Backing is not the same as arriving.</strong> An independent expenditure spent
<em>against</em> a member reaches that member's seat but is not money backing them, and a campaign
repaying a loan to its own candidate is not industry money at all. Section A ranks on supporting
money only.</p>
<p><strong>Committee weighting.</strong> Section A multiplies a member&rsquo;s supporting money by how
much they matter to this channel. Every committee of ours they sit on contributes a share, and that
share is itself two things multiplied: how central the committee is to us &mdash; 1.0 for our
first-ranked one, falling in equal steps to the last &mdash; times how senior the member is on it,
scaled against that committee&rsquo;s own roster so rank 5 of 36 counts as senior and rank 5 of 6 does
not. Chairs and ranking members both sit at the top of their side. The shares are then ADDED, so
sitting on four of our committees counts for more than sitting on one, and seat count, committee
standing and personal seniority all move one figure with no separate dial for any of them. A member holding none of
our committees scores zero and is left out of Section A entirely; their money still counts in the
channel total and they still appear in Section D. The ranking itself is hand-reviewed, not
derived from dollars, so this cannot become a circular measure of who already gives the most.</p>
<p><strong>Coverage.</strong> In {QL}, {ALL} in candidate contributions was disclosed in total; this
edition selects the part reaching sitting members of the committees {CH} covers, and Section D sets
the challenger money for those same seats alongside it.</p>
<p><strong>Relationship to the send edition.</strong> The two are not two halves of one total and
should not be added. This edition selects money whose RECIPIENT sits on a {CH} seat, whoever sent it;
the send edition selects money whose SENDER is a {CH} committee, whoever received it. In {QL} they
share {SHARED} transactions &mdash; {SR}% of this edition's rows and {SS}% of the send edition's.
Source: FEC bulk disclosure files and the unitedstates/congress-legislators dataset.</p>
""" + METHOD_COMMON


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--channel", required=True)
    ap.add_argument("--period", default=config.DEFAULT_PERIOD,
                    help=f"a month (2026-06) or a quarter (2026Q2); "
                         f"default {config.DEFAULT_PERIOD}")
    ap.add_argument("--only", choices=["send", "receive", "both"], default="both",
                    help="skip the other edition and the data it needs")
    ap.add_argument("--out", type=Path, default=REPO / "out" / "ghost")
    args = ap.parse_args()

    print(f"== computing {args.channel} {args.period} ==")
    F = build(args.channel, args.period)
    sl = slug(args.channel)
    ps = slug(args.period)
    # The dashboard builders return presentation extras; the counts come from the JSON
    # their generators wrote, which is the single source for both.
    J = lambda stem: json.loads(
        (config.REPO_ROOT / "cache" / f"{stem}_{sl}_{ps}.json").read_text())
    want_send = args.only in ("send", "both")
    want_recv = args.only in ("receive", "both")
    # The receive edition needs the roster and the challenger pairings, and those are the
    # slow builds. Asking for one edition should not pay for the other's data.
    send_ui = ({"dash": J("recipient_table"),
                "dash_html": ui.recipient_dashboard(args.channel, args.period)[0]}
               if want_send else None)
    recv_ui = ({"roster": J("committee_roster"),
                "roster_html": ui.roster_dashboard(args.channel, args.period)[0],
                "pairs": J("challenger_pairs"),
                "pairs_html": ui.challenger_dashboard(args.channel, args.period)[0]}
               if want_recv else None)
    SEND, RECV = render(F, send_ui, recv_ui)

    args.out.mkdir(parents=True, exist_ok=True)
    outputs = ([("SEND", SEND)] if want_send else []) + \
              ([("RECEIVE", RECV)] if want_recv else [])
    for side, content in outputs:
        p = ui.edition_path(args.channel, args.period, side, root=args.out)
        p.write_text(content, encoding="utf-8")
        print(f"wrote {p}  ({len(content):,} bytes)")
    print(f"\n  send ${F['send_total']:,.0f} / {F['senders']} senders   "
          f"receive ${F['recv_total']:,.0f} / {F['members']} members")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
