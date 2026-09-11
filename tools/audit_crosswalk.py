#!/usr/bin/env python3
"""Flag rows in congress_cmte_crosswalk.csv that deserve a human's second look.

Two mapping errors have shipped so far and both had the same shape: a subcommittee
whose jurisdiction is procedural rather than substantive picked up a substantive
channel label, and because that row was the ONLY route some member had into the
channel, it pulled real money into an edition it did not belong in. HSJU03 did it with
$651,100 of Kentucky money; seven "Oversight and Investigations" rows did it with
$877,147. Neither was visible in the file itself -- both surfaced only when a reader
recognised a committee name that had no business being there.

This makes the same suspicion computable. Nothing here is an error by itself; every
check is a question worth answering once, and the money column says how much the
answer is worth.

    python tools/audit_crosswalk.py                 # all checks, ranked by money
    python tools/audit_crosswalk.py --channel "Finance & Insurance"
"""
from __future__ import annotations
import argparse, re, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pandas as pd
from fec_newsletter import common, config, congress, extract, pipelines, sources

# Subcommittees whose remit is process, not subject matter. Their jurisdiction is
# whatever the parent committee's is, so a channel label here has to be justified by
# the PARENT, never assigned on its own.
_PROC_WORDS = re.compile(
    r"oversight|investigation|investigations|administrative|procedure|procedures|"
    r"modernization|accountability|reform|innovation", re.I)
_STOP = {"and", "the", "of", "or", "a", "an"}


def is_procedural(name: str) -> bool:
    """True when a subcommittee's name is ALL process and no subject.

    A word-match is not enough: an Appropriations subcommittee called "Agriculture,
    Rural Development, Food and Drug Administration, and Related Agencies" contains
    "Administration" as part of an agency's name while being entirely substantive --
    its title IS its jurisdiction. So strip the process words and see whether a subject
    survives; "Oversight and Investigations" leaves nothing, and that is the shape that
    has caused every mapping error so far.
    """
    tokens = [t for t in re.split(r"[^A-Za-z]+", name) if t and t.lower() not in _STOP]
    rest = [t for t in tokens if not _PROC_WORDS.fullmatch(t)]
    # Zero survivors, not one: a single-word name like "Tax", "Health" or "Trade" is
    # entirely substantive, and "<= 1" wrongly condemned every one of them.
    return bool(tokens) and not rest

# Words that make a channel plausible on the face of the name. Absence is not proof of
# error -- a jurisdiction is not always in the title -- but presence is reassurance,
# and its absence is what we want a human to confirm.
CUES = {
    "Finance & Insurance": r"financ|bank|capital|securit|insur|credit|tax|monetar|currenc|"
        r"derivativ|commodit|digital asset|fintech|housing|pension|social security|"
        r"econom|invest|antitrust|consumer|illicit|money|IRS|fiscal",
    "Healthcare": r"health|medic|drug|pharma|hospital|care|disease|biotech|nutrition|"
        r"food safety|retirement|disability|primary",
    "Defense": r"defen|armed|military|nuclear|weapon|veteran|intellig|terror|"
        r"national security|strategic|tactical|readiness|cyber|homeland|emerging threat",
    "Oil & Gas": r"energy|oil|gas|petrol|fuel|pipeline|mineral|extract",
    "Utilities & Power": r"energy|power|electric|utilit|grid|nuclear|water|renewab",
    "Agri-Food": r"agricultur|farm|crop|livestock|dairy|poultry|forest|rural|commodit|"
        r"food|nutrition|horticultur|conservation|organic|specialty crop",
    "Freight & Transport": r"transport|aviation|rail|highway|maritime|coast guard|port|"
        r"freight|vehicle|traffic|infrastructur|trade|customs|logistic|surface",
    "ICT & Cybersecurity": r"technolog|cyber|information|digital|communicat|internet|"
        r"innovation|research|data|artificial intelligence|space|broadband|spectrum|modern",
    "Environment": r"environment|climate|clean|pollut|conservation|wildlife|water|"
        r"forest|natural resource|park|public land|emission",
    "Manufacturing": r"manufactur|industr|commerce|production|supply chain|trade|"
        r"innovation|competitiv",
    "Construction & Housing": r"housing|construct|building|infrastructur|urban|"
        r"development|real estate|public works",
    "Higher Ed": r"educat|school|student|university|college|workforce|training|labor",
    "Mining": r"mining|mineral|coal|extract|geolog",
    "Fisheries": r"fisher|ocean|marine|coast|aquacultur|wildlife|water",
    "Automotive": r"auto|vehicle|motor|highway|traffic|transport",
    "Hospitality & Retail": r"retail|hospitality|tourism|travel|consumer|small business|"
        r"restaurant|commerce",
    "Social Issues": r"",          # deliberately broad; a catch-all cue would flag nothing
    "Tribal Affairs": r"indian|tribal|native|insular|indigenous",
    "Foreign Affairs": r"foreign|international|global|multilateral|diplomat|state department|"
        r"europe|asia|africa|hemisphere|near east|world",
}


def rows_with_codes(mapping: pd.DataFrame) -> pd.DataFrame:
    m = mapping.copy()
    sub = m["subcommittee_thomas_id"].fillna("").astype(str).str.strip()
    m["code"] = m["thomas_id"].str.strip() + sub.str.zfill(2).where(sub != "", "")
    m["is_sub"] = sub != ""
    return m


def money_by_code(quarter: str) -> tuple[dict, dict]:
    """(code, channel) -> money whose ONLY route into that channel is that code.

    This is the number that turns a debatable label into a priced decision. A row that
    is one of several routes into a channel changes weights; a row that is somebody's
    ONLY route decides whether their money appears at all.
    """
    data = extract.load({n: config.CACHE_DIR / c for n, (_, c) in config.EXTRACTS.items()})
    recv, _ = pipelines.run_receive(data)
    r = recv[recv["quarter"].astype(str) == quarter].copy()
    memb = congress.current_memberships(data["membership"])
    fecmap = congress.fec_id_map(data["legislators"])
    r["bioguide"] = r["cand_id"].map(dict(zip(fecmap["cand_id"], fecmap["bioguide"])))
    cn = congress.committee_newsletters()
    seats = memb.groupby("bioguide")["committee"].apply(set).to_dict()
    sole_money, sole_members = {}, {}
    for channel, grp in cn.groupby("newsletter"):
        chan = set(grp["code"])
        rc = r[r["newsletter"] == channel]
        if rc.empty:
            continue
        for code in chan:
            others = chan - {code, code[:4]} if len(code) > 4 else chan - {code}
            only = {b for b, cs in seats.items() if code in cs and not (cs & others)}
            if not only:
                continue
            amt = rc.loc[rc["bioguide"].isin(only), "amount"].sum()
            if amt:
                sole_money[(code, channel)] = float(amt)
                sole_members[(code, channel)] = len(only)
    return sole_money, sole_members


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarter", default="2026Q2")
    ap.add_argument("--channel", help="restrict the report to one channel")
    ap.add_argument("--min-money", type=float, default=0)
    ap.add_argument("--csv", type=Path, help="also write the findings to this path")
    args = ap.parse_args()

    m = rows_with_codes(congress.load_mapping())
    sole_money, sole_members = money_by_code(args.quarter)
    cn = congress.committee_newsletters()
    by_code = cn.groupby("code")["newsletter"].apply(set).to_dict()

    # Label hygiene is file-wide, not per row: a label used once or twice where a
    # near-identical spelling is used often is a typo, and a typo attributes nothing.
    counts = {}
    for v in m["newsletters"]:
        for x in str(v).replace("/", "|").split("|"):
            x = x.strip()
            if x:
                counts[x] = counts.get(x, 0) + 1
    norm = {}
    for lab in counts:
        norm.setdefault(re.sub(r"[^a-z]", "", lab.lower()), []).append(lab)
    variants = {l: sorted(v, key=lambda x: -counts[x])
                for k, v in norm.items() if len(v) > 1 for l in v}

    findings = []

    def add(code, kind, why, row, channel=""):
        findings.append({
            "code": code, "check": kind, "channel": channel,
            "committee": str(row["name"])[:42],
            "subcommittee": str(row.get("subcommittee_name") or "")[:42],
            "labels": str(row["newsletters"]),
            "sole_money": sole_money.get((code, channel), 0.0),
            "sole_members": sole_members.get((code, channel), 0),
            "why": why})

    for _, row in m.iterrows():
        code, name = row["code"], str(row.get("subcommittee_name") or "")
        parent = str(row["name"])
        labels = str(row["newsletters"]).strip()
        chans = by_code.get(code, set())
        if args.channel:
            chans = {c for c in chans if c == args.channel}

        if row["is_sub"] and not labels:
            add(code, "unmapped", "no channel label, so this subcommittee attributes "
                                  "nothing to any edition", row)
            continue
        for lab in [x.strip() for x in labels.replace("/", "|").split("|") if x.strip()]:
            if lab in variants and variants[lab][0] != lab:
                add(code, "label-variant",
                    f"'{lab}' used {counts[lab]}x; '{variants[lab][0]}' used "
                    f"{counts[variants[lab][0]]}x for the same thing", row)

        if not row["is_sub"]:
            continue

        for ch in sorted(chans):
            cue = CUES.get(ch)
            if not cue:
                continue
            if is_procedural(name) and not re.search(cue, parent, re.I):
                add(code, "procedural-subcommittee",
                    f"'{name}' is a process remit, and nothing in the parent committee "
                    f"supports '{ch}'", row, ch)
            elif not re.search(cue, name + " " + parent, re.I):
                add(code, "no-jurisdiction-cue",
                    f"nothing in either name suggests '{ch}'", row, ch)

        if len(by_code.get(code, set())) >= 4 and not args.channel:
            n = len(by_code[code])
            add(code, "diffuse", f"{n} channels on one subcommittee, so each receives "
                                 f"1/{n} of every dollar reaching its members", row)

    seen, out = set(), []
    for f in findings:
        k = (f["code"], f["check"], f["why"])
        if k not in seen:
            seen.add(k); out.append(f)
    out = [f for f in out if f["sole_money"] >= args.min_money]
    out.sort(key=lambda f: (-f["sole_money"], f["check"], f["code"]))

    scope = args.channel or "all channels"
    print(f"crosswalk audit — {scope}, {args.quarter}")
    print(f"{len(m)} rows, {int(m['is_sub'].sum())} subcommittees, {len(out)} flagged\n")
    by_check = {}
    for f in out:
        by_check.setdefault(f["check"], []).append(f)
    for check in ("procedural-subcommittee", "no-jurisdiction-cue", "unmapped",
                  "label-variant", "diffuse"):
        group = by_check.get(check, [])
        if not group:
            continue
        spend = sum(f["sole_money"] for f in group)
        print(f"\n{'=' * 96}\n{check.upper()}  ({len(group)} rows"
              + (f", ${spend:,.0f} resting on them alone" if spend else "") + ")\n"
              + "=" * 96)
        for f in group:
            money = f"${f['sole_money']:,.0f}" if f["sole_money"] else "—"
            print(f'{f["code"]:<8}{money:>12}{(" " + str(f["sole_members"]) + " mem") if f["sole_members"] else "":>8}'
                  f'  {f["committee"]}')
            print(f'{"":<8}{"":>12}{"":>8}  {f["subcommittee"] or "(top level)"}   [{f["labels"]}]')
            print(f'{"":<8}{"":>12}{"":>8}  -> {f["why"]}')
    total = sum(f["sole_money"] for f in out)
    print(f"\n{'=' * 96}\nmoney resting on a flagged row alone, this quarter: ${total:,.0f}")
    if args.csv:
        pd.DataFrame(out).to_csv(args.csv, index=False)
        print(f"written -> {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
