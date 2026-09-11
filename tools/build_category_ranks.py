#!/usr/bin/env python3
"""Rank every committee and subcommittee by policy importance WITHIN each newsletter.

The crosswalk answers "does this seat belong to this channel". It does not answer "how
much does this seat matter to it", and those are different questions: House Energy and
Commerce's Health subcommittee and the Foreign Affairs Global Health subcommittee both
map to Healthcare, and only one of them writes American health law.

The ranking here is authored by jurisdiction, deliberately NOT by money. Ranking by
dollars would make the file circular -- it would tell you the channel matters most to
whoever already gives it the most -- and would rewrite itself every quarter.

    python tools/build_category_ranks.py            # -> newsletter_committee_ranks.csv
    python tools/build_category_ranks.py --check    # validate against the crosswalk only

Committees and subcommittees are ranked on the SAME scale. A full committee is not
automatically less significant than its own subcommittees -- House Armed Services matters
more to defence policy than any one of its six panels -- so a committee row is tiered on
what it actually governs, and often outranks the subcommittees beneath it. Where a
committee reaches a channel only through one narrow panel, it ranks below that panel.

TIERS
  1 defining      the primary legislative venue for this channel; for a subcommittee, the
                  channel IS its named jurisdiction
  2 core          direct jurisdiction over a major part of the channel, but the channel is
                  one of several substantial remits
  3 adjacent      real but partial overlap; the channel is a corner of what it does
  4 peripheral    reaches the channel through a broader, procedural, funding or foreign
                  remit rather than through its own subject matter

A blank `subcommittee` means the row IS the whole committee. In attribution those rows
apply to a member who holds no mapped subcommittee seat on that committee -- 202 members
today -- but the rank here is the committee's policy weight, not that mechanic.
"""
from __future__ import annotations
import argparse, sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import pandas as pd
from fec_newsletter import congress

TIER_NAME = {1: "defining", 2: "core", 3: "adjacent", 4: "peripheral"}

ORDER: dict[str, list[tuple[str, int]]] = {
"Agri-Food": [
    ("HSAG",1),("SSAF",1),("HSAG16",1),("SSAF17",1),("HSAG29",1),("SSAF16",1),("HSAG03",1),
    ("SSAF13",2),("HSAG22",2),("SSAF14",2),("HSAG14",2),("HSAG15",2),
    ("SSAF15",3),("HSAP01",3),("SSAP01",3),("HSAP",3),("SSAP",3),
    ("HSSM21",4),("HSII13",4),("SSEV15",4),("HSSM",4),("HSII",4),("SSEV",4)],
"Automotive": [
    ("HSPW",1),("HSPW12",1),("SSCM38",1),
    ("SSCM",2),("SSEV08",2),
    ("SSEV",3),("HSAP20",3),("SSAP24",3),
    ("HSAP",4),("SSAP",4)],
"Construction & Housing": [
    ("SSBK",1),("HSBA04",1),("SSBK09",1),
    ("HSBA",2),("HSPW13",2),("SSEV08",2),
    ("HSPW",3),("HSPW12",3),("HSSM23",3),("HSAP20",3),("SSAP24",3),("SSEV",3),
    ("HSSM",4),("HSAP",4),("SSAP",4)],
"Defense": [
    ("HSAS",1),("SSAS",1),
    ("HSAS25",1),("HSAS28",1),("HSAS29",1),("HSAS03",1),("HSAS02",1),("HSAS26",1),
    ("SSAS14",1),("SSAS13",1),("SSAS16",1),("SSAS15",1),("SSAS17",1),("SSAS20",1),
    ("SLIN",2),("HLIG",2),("HSAP02",2),("SSAP02",2),
    ("HLIG04",2),("HLIG01",2),("HLIG02",2),("HLIG06",2),("HSHM",2),("HSHM05",2),
    ("HSZS",3),
    ("SSJU22",3),("HSJU08",3),("HSHM11",3),("SSJU04",3),("SSGA22",3),("HSHM07",3),
    ("HSPW07",3),("SSCM36",3),("HSAP04",3),("SSAP20",3),("HSAP15",3),("SSAP14",3),
    ("HSAP18",3),("SSAP19",3),("HSGO06",3),("HSAP",3),("SSAP",3),("SSFR",3),("HSFA",3),
    ("HSFA17",4),("SSFR01",4),("SSFR07",4),("SSFR06",4),("SSFR14",4),
    ("HSBA10",4),("SSBK05",4),("HSGO33",4),
    ("SSGA",4),("HSJU",4),("SSJU",4),("HSGO",4),("HSPW",4),("SSCM",4),("HSBA",4),("SSBK",4)],
"Environment": [
    ("SSEV",1),("HSII",1),
    ("HSIF18",1),("SSEV09",1),("SSEV10",1),("HSPW02",1),("SSEV15",1),("HSII13",1),
    ("SSEG",2),("HSII10",2),("SSEG03",2),("SSEG04",2),("HSSY18",2),("SSAF14",2),
    ("HSIF",2),("HSPW",2),
    ("HSAG15",3),("SSCM36",3),("SSCM38",3),("HSAP06",3),("SSAP17",3),("HSSY",3),("SSAF",3),
    ("HSAG",4),("HSAP10",4),("SSFR15",4),("SSCM",4),("HSAP",4),("SSAP",4),("SSFR",4)],
"Finance & Insurance": [
    ("HSBA",1),("SSBK",1),("HSWM",1),("SSFI",1),
    ("HSBA16",1),("HSBA20",1),("SSBK04",1),("SSBK08",1),("HSWM05",1),("SSFI11",1),
    ("HSBA21",1),("SSBK13",1),
    ("JSTX",2),("HSBU",2),("SSBU",2),("JSEC",2),
    ("HSBA04",2),("SSBK12",2),("SSFI14",2),("HSBA10",2),("SSBK05",2),("HSWM01",2),
    ("SSFI02",2),("HSSM27",2),
    ("SSJU01",3),("HSJU05",3),("HSWM04",3),("SSFI13",3),("HSAG16",3),("HSAG22",3),
    ("SSAF13",3),("SSHR12",3),("HSAP23",3),("SSAP23",3),("HSSM",3),
    ("SSAF15",4),("HSGO27",4),("HSBA09",4),("HSGO12",4),("HSGO05",4),("HSIF17",4),
    ("SSJU",4),("HSJU",4),("HSGO",4),("HSAG",4),("SSAF",4),("HSIF",4),("SSHR",4),
    ("HSAP",4),("SSAP",4)],
"Fisheries": [
    ("SSCM36",1),("HSII13",1),("SSEV15",1),("SSCM",2),("HSII",3),("SSEV",3)],
"Foreign Affairs": [
    ("HSFA",1),("SSFR",1),
    ("SSFR01",1),("HSFA14",1),("SSFR07",1),("HSFA13",1),("SSFR02",1),("HSFA05",1),("SSFR14",1),
    ("HSFA07",2),("SSFR06",2),("HSFA16",2),("SSFR09",2),("HSFA19",2),("SSFR15",2),
    ("HSFA06",2),("JCSE",2),
    ("HSFA17",3)],
"Freight & Transport": [
    ("HSPW",1),("SSCM",1),
    ("SSCM38",1),("HSPW05",1),("SSCM33",1),("HSPW07",1),
    ("SSCM36",2),("HSHM07",2),
    ("HSHM",3),("HSWM04",3),("SSFI13",3),("SSCM39",3),
    ("HSWM",4),("SSFI",4)],
"Healthcare": [
    ("SSHR",1),("HSIF14",1),("HSWM02",1),("SSFI10",1),("SSHR12",1),
    ("HSIF",2),("HSWM",2),("SSFI",2),("HSVR",2),("SSVA",2),
    ("HSED02",2),("HSVR03",2),("HSAP07",2),("SSAP18",2),("SPAG",2),
    ("HSED",3),("SCNC",3),("HSAP01",3),("SSAP01",3),("HSGO27",3),("HSVR09",3),
    ("HSWM03",3),("HSAP",3),("SSAP",3),
    ("HSFA06",4),("SSFR09",4),("HSGO",4),("HSFA",4),("SSFR",4)],
"Higher Ed": [
    ("HSED",1),("SSHR",1),("HSED13",1),("SSHR09",1),
    ("HSED14",2),("HSAP07",2),("SSAP18",2),
    ("HSAP",3),("SSAP",3),("HSSM22",3),("HSSM",4)],
"Hospitality & Retail": [
    ("HSIF17",1),("SSSB",2),("HSIF",3)],
"ICT & Cybersecurity": [
    ("SSCM",1),("HSSY",1),
    ("HSIF16",1),("SSCM34",1),("SSCM35",1),("HSGO12",1),("HSHM08",1),("SSAS21",1),
    ("HSAS35",1),("HSJU03",1),("SSJU28",1),("SSJU26",1),("HSSY15",1),("HLIG02",1),
    ("HSIF",2),("HSHM",2),("HLIG",2),("SLIN",2),
    ("HSBA21",2),("SSBK13",2),("HLIG11",2),("SSAS20",2),("HSHM12",2),("HSSY16",2),
    ("SSCM33",2),("HSVR11",2),("HSHA27",2),("SSJU01",2),("SSFR02",2),
    ("HSJU",2),("SSJU",2),("HSGO",2),
    ("HSZS",3),
    ("HSAS29",3),("SSAS16",3),("HSAS26",3),("HLIG01",3),("HLIG04",3),("HLIG06",3),
    ("HSHM05",3),("HSJU08",3),("SSJU22",3),("SSJU25",3),("HSFA17",3),("HSAG22",3),
    ("HSSM22",3),("HSHA08",3),("HSAP19",3),("SSAP16",3),("HSAP15",3),("SSAP14",3),
    ("HSHA",3),("HSVR",3),("HSAS",3),("SSAS",3),
    ("HSJU01",4),("SSGA01",4),("SSBK05",4),("HSIF02",4),("HSSY21",4),("HSHA06",4),
    ("SSAP08",4),
    ("HSBA",4),("SSBK",4),("HSSM",4),("HSAG",4),("HSFA",4),("SSFR",4),("SSGA",4),
    ("HSAP",4),("SSAP",4)],
"Manufacturing": [
    ("SSCM37",1),("HSIF17",1),
    ("SSCM",2),("HSWM04",2),
    ("HSSM21",3),("HSSM22",3),("HSIF",3),("HSSM",3),
    ("HSWM",4)],
"Mining": [
    ("HSII06",1),("SSEG03",1),("HSII",2),("SSEG",2),("SSFI12",3),("SSFI",4)],
"Oil & Gas": [
    ("SSEG",1),("HSIF03",1),("SSEG01",1),("HSII06",1),
    ("HSIF",2),("HSII",2),("HSPW14",2),("HSSY20",2),
    ("SSAP22",3),("SSFI12",3),("HSPW",3),("HSSY",3),("SSAP",3),
    ("HSSM21",4),("HSGO05",4),("SSFI",4),("HSSM",4),("HSGO",4)],
"Social Issues": [
    ("HSJU",1),("SSJU",1),
    ("HSJU10",1),("SSJU21",1),("SSJU27",1),("HSWM03",1),("HSWM01",1),("HSED10",1),
    ("SSHR11",1),("HSED02",1),("HSJU01",1),("SSJU04",1),("HSHA08",1),
    ("HSED",2),("SSHR",2),("HSVR",2),("SSVA",2),("SPAG",2),("HSHA",2),
    ("HSHM11",2),("SSGA22",2),("HSED13",2),("HSED14",2),("SSHR09",2),("HSII24",2),
    ("HSVR09",2),("HSVR10",2),("SSGA20",2),("SSJU25",2),("HSFA06",2),("SSFR06",2),
    ("HSWM",3),("SSGA",3),("HSHM",3),("HSRU",3),("SCNC",3),("HSII",3),
    ("SSJU28",3),("HSJU05",3),("HSRU02",3),("HSRU04",3),("SSBK09",3),("HSAP07",3),
    ("SSAP18",3),("HSFA05",3),("HSFA07",3),("HSFA13",3),("HSFA14",3),("HSFA16",3),
    ("HSFA19",3),("SSFR01",3),("SSFR02",3),("SSFR07",3),("SSFR09",3),("SSFR14",3),
    ("SSFR15",3),("HSBU",3),("SSBU",3),("HSFA",3),("SSFR",3),
    ("HSAP24",4),("SSAP08",4),("HSAP19",4),("SSAP16",4),("HSAP18",4),("SSAP19",4),
    ("HSAP04",4),("SSAP20",4),("HSGO24",4),("HSGO16",4),("SSGA01",4),("HSBA09",4),
    ("HSIF02",4),("HSII15",4),("HSJU13",4),("HSWM06",4),("HSSM24",4),("HSSY21",4),
    ("HSVR08",4),("HSHM09",4),("HSHA06",4),("HLIG09",4),("HSFA17",4),
    ("HSBA",4),("SSBK",4),("HSIF",4),("HSGO",4),("HSSM",4),("HSSY",4),("HLIG",4),
    ("HSAP",4),("SSAP",4)],
"Tribal Affairs": [
    ("SLIA",1),("HSII24",1),("HSII",2),("HSII10",3)],
"Utilities & Power": [
    ("SSEG",1),("SSEG07",1),("HSIF03",1),("SSEG01",1),("SSEV10",1),
    ("HSIF",2),("SSEV",2),("HSPW",2),
    ("HSPW14",2),("HSSY20",2),("HSII06",2),("HSPW02",2),("SSEV09",2),("HSAP10",2),("SSAP22",2),
    ("HSII",3),("HSSY",3),("HSAP",3),("SSAP",3),
    ("SSAF15",3),("HSHM12",3),("HSPW13",3),("SSGA20",3),("SSFI12",3),
    ("SSAF",4),("HSHM",4),("SSGA",4),("SSFI",4),
    ("HSSM21",4),("HSGO05",4),("SSFR15",4),("HSSM",4),("HSGO",4),("SSFR",4)],
}

# (newsletter, code) pairs the crosswalk produces but that a review judged do NOT
# belong in that category. Held here rather than deleted from ORDER so the validator
# can still tell "deliberately dropped" from "nobody has ranked this yet" -- a new
# crosswalk label must be either ranked or excluded, never silently absent.
EXCLUDED: dict[str, set[str]] = {
    "Agri-Food": {
        "HSAP", "HSII", "HSSM", "SSAP", "SSEV"},
    "Automotive": {
        "HSAP", "HSAP20", "SSAP", "SSAP24", "SSEV"},
    "Construction & Housing": {
        "HSAP", "HSBA", "HSPW", "HSSM", "SSAP", "SSEV"},
    "Defense": {
        "HLIG", "HSAP", "HSAP15", "HSBA", "HSBA10", "HSFA", "HSFA17", "HSGO", "HSGO33",
        "HSHM", "HSHM05", "HSHM07", "HSHM11", "HSJU", "HSJU08", "HSPW", "SLIN", "SSAP",
        "SSAP14", "SSBK", "SSBK05", "SSCM", "SSFR", "SSFR01", "SSFR06", "SSFR07",
        "SSFR14", "SSGA", "SSGA22", "SSJU", "SSJU04", "SSJU22"},
    "Environment": {
        "HSAG", "HSAP", "HSAP06", "HSIF", "HSPW", "HSSY", "SSAF", "SSAP", "SSAP17",
        "SSFR"},
    "Finance & Insurance": {
        "HSAG", "HSAP", "HSAP23", "HSBU", "HSGO", "HSIF", "HSJU", "HSSM", "HSSM27",
        "HSWM", "HSWM01", "HSWM04", "JSEC", "JSTX", "SSAF", "SSAP", "SSAP23", "SSBU",
        "SSFI02", "SSFI13", "SSHR", "SSHR12", "SSJU"},
    "Fisheries": {
        "HSII", "SSCM", "SSEV"},
    "Foreign Affairs": {
        "JCSE"},
    "Freight & Transport": {
        "HSHM", "HSWM", "SSCM", "SSFI"},
    "Healthcare": {
        "HSAP", "HSAP01", "HSAP07", "HSED", "HSED02", "HSFA", "HSFA06", "HSGO",
        "HSGO27", "HSIF", "HSVR", "HSVR09", "HSWM", "HSWM03", "SCNC", "SSAP", "SSAP01",
        "SSAP18", "SSFI", "SSFR", "SSFR09", "SSVA"},
    "Higher Ed": {
        "HSAP", "HSAP07", "SSAP", "SSAP18"},
    "Hospitality & Retail": {
        "HSIF"},
    "ICT & Cybersecurity": {
        "HLIG", "HLIG01", "HLIG04", "HLIG06", "HSAG", "HSAG22", "HSAP", "HSAP15",
        "HSAP19", "HSAS", "HSAS26", "HSAS29", "HSBA", "HSFA", "HSFA17", "HSGO", "HSHA",
        "HSHA06", "HSHA08", "HSHM", "HSHM05", "HSIF", "HSIF02", "HSJU", "HSJU01",
        "HSJU08", "HSSM", "HSSM22", "HSSY21", "HSVR", "HSZS", "SLIN", "SSAP", "SSAP08",
        "SSAP14", "SSAP16", "SSAS", "SSAS16", "SSBK", "SSBK05", "SSCM", "SSFR", "SSGA",
        "SSGA01", "SSJU", "SSJU22", "SSJU25"},
    "Manufacturing": {
        "HSIF", "HSSM", "HSWM"},
    "Mining": {
        "HSII", "SSEG", "SSFI"},
    "Oil & Gas": {
        "HSGO", "HSIF", "HSII", "HSPW", "HSSM", "HSSY", "SSAP", "SSEG", "SSFI"},
    "Social Issues": {
        "HLIG", "HLIG09", "HSAP", "HSAP04", "HSAP07", "HSAP18", "HSAP19", "HSAP24",
        "HSBA", "HSBA09", "HSBU", "HSED13", "HSFA", "HSFA05", "HSFA06", "HSFA07",
        "HSFA13", "HSFA14", "HSFA16", "HSFA17", "HSFA19", "HSGO", "HSGO16", "HSGO24",
        "HSHA", "HSHA06", "HSHM", "HSHM09", "HSHM11", "HSIF", "HSIF02", "HSII",
        "HSII15", "HSJU", "HSJU05", "HSJU13", "HSRU", "HSRU02", "HSRU04", "HSSM",
        "HSSM24", "HSSY", "HSSY21", "HSVR", "HSVR08", "HSVR10", "HSWM", "HSWM06",
        "SCNC", "SPAG", "SSAP", "SSAP08", "SSAP16", "SSAP18", "SSAP19", "SSAP20",
        "SSBK", "SSBK09", "SSBU", "SSFR", "SSFR01", "SSFR02", "SSFR06", "SSFR07",
        "SSFR09", "SSFR14", "SSFR15", "SSGA", "SSGA01", "SSGA20", "SSGA22", "SSJU",
        "SSJU28", "SSVA"},
    "Utilities & Power": {
        "HSAP", "HSGO", "HSGO05", "HSHM", "HSIF", "HSII", "HSPW", "HSSM", "HSSM21",
        "HSSY", "SSAF", "SSAP", "SSEG", "SSEV", "SSFI", "SSFI12", "SSFR", "SSFR15",
        "SSGA"},
}

# Why a row is here when the subcommittee's own name does not say so. Absent = self-evident.
NOTES: dict[tuple[str, str], str] = {
    ("Agri-Food","HSII13"): "wildlife and water remit, not food policy -- weakest route in",
    ("Agri-Food","SSEV15"): "wildlife and water remit, not food policy -- weakest route in",
    ("Agri-Food","HSAP01"): "funds USDA and FDA; no authorising jurisdiction",
    ("Agri-Food","SSAP01"): "funds USDA and FDA; no authorising jurisdiction",
    ("Defense","HSAP02"): "funds the entire department; the single largest defence purse",
    ("Defense","SSAP02"): "funds the entire department; the single largest defence purse",
    ("Defense","HSBA10"): "sanctions and illicit finance, not force structure",
    ("Defense","SSBK05"): "sanctions and export controls, not force structure",
    ("Defense","HSGO33"): "federal law enforcement, not the armed forces",
    ("Environment","HSAP10"): "energy and water construction; environment is incidental",
    ("Environment","SSFR15"): "international environmental policy only",
    ("Finance & Insurance","HSIF17"): "consumer commerce; the finance link is thin",
    ("Finance & Insurance","HSGO05"): "regulatory oversight, not financial jurisdiction",
    ("Finance & Insurance","HSAG16"): "crop insurance and farm credit",
    ("Finance & Insurance","SSAF13"): "agricultural derivatives and CFTC-adjacent markets",
    ("Finance & Insurance","SSHR12"): "retirement security half of the remit",
    ("Freight & Transport","HSWM04"): "tariff policy, not logistics -- largest route into "
                                      "the channel and the least transport-like",
    ("Freight & Transport","SSFI13"): "customs and tariff policy, not logistics",
    ("Freight & Transport","SSCM39"): "trade promotion; no sitting members hold this code",
    ("Healthcare","HSFA06"): "global health, not domestic health policy",
    ("Healthcare","SSFR09"): "global health, not domestic health policy",
    ("Healthcare","HSAP01"): "funds the FDA",
    ("Healthcare","SSAP01"): "funds the FDA",
    ("Higher Ed","HSSM22"): "workforce development through a small-business lens",
    ("ICT & Cybersecurity","HSHA08"): "election security and voting systems",
    ("ICT & Cybersecurity","HSJU01"): "immigration enforcement technology",
    ("ICT & Cybersecurity","SSAP08"): "funds congressional IT; the thinnest route in",
    ("ICT & Cybersecurity","HSIF02"): "process remit; inherits from a technology parent",
    ("ICT & Cybersecurity","HSSY21"): "process remit; inherits from a technology parent",
    ("ICT & Cybersecurity","HSHA06"): "process remit; no sitting members hold this code",
    ("Mining","SSFI12"): "mineral taxation, not mining policy -- majority of the channel "
                         "arrives through this row alone",
    ("Oil & Gas","HSGO05"): "regulatory oversight of energy policy",
    ("Oil & Gas","HSSM21"): "rural energy through a small-business lens",
    ("Oil & Gas","SSFI12"): "energy taxation",
    ("Social Issues","HSAP24"): "funds Congress itself",
    ("Tribal Affairs","HSII10"): "federal lands overlap tribal land, but this is not the "
                                 "tribal subcommittee -- most of the channel arrives here",
    ("Utilities & Power","SSAF15"): "rural electrification",
    ("Utilities & Power","HSHM12"): "grid and infrastructure resilience",
    ("Utilities & Power","SSGA20"): "disaster response to utility outages",
    ("Utilities & Power","SSFR15"): "international energy policy only",
}

def build() -> pd.DataFrame:
    mapping = congress.load_mapping()
    cn = congress.committee_newsletters(mapping)
    pname = {r["thomas_id"].strip(): str(r["name"]) for _, r in mapping.iterrows()}
    sname = {}
    for _, r in mapping.iterrows():
        s = r["subcommittee_thomas_id"].strip()
        if s:
            sname[r["thomas_id"].strip() + s.zfill(2)] = str(r["subcommittee_name"])

    actual = {(n, c) for n, c in zip(cn["newsletter"], cn["code"])}
    authored = {(n, c) for n, rows in ORDER.items() for c, _ in rows}
    dropped = {(n, c) for n, codes in EXCLUDED.items() for c in codes}
    # ORDER stays COMPLETE -- it ranks every pair the crosswalk produces. EXCLUDED is a
    # filter applied on top, so that dropping a pair from the output never loses the
    # judgement about where it would have ranked.
    if missing := sorted(actual - authored):
        raise SystemExit(f"{len(missing)} crosswalk pairs are not ranked: {missing[:12]}")
    if extra := sorted(authored - actual):
        raise SystemExit(f"{len(extra)} ranked pairs are not in the crosswalk: {extra[:12]}")
    if orphan := sorted(dropped - authored):
        raise SystemExit(f"{len(orphan)} excluded pairs are not ranked: {orphan[:12]}")

    out = []
    for nl in sorted(ORDER):
        rank = 0
        for code, tier in ORDER[nl]:
            if code in EXCLUDED.get(nl, ()):
                continue
            rank += 1
            out.append({
                "newsletter": nl, "rank": rank,
                "committee": pname.get(code[:4], ""),
                # blank when the row is the committee itself, not a seat within it
                "subcommittee": sname.get(code, ""),
                "tier": tier, "code": code,
                "note": NOTES.get((nl, code), ""),
            })
    return pd.DataFrame(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", type=Path, default=Path("newsletter_committee_ranks.csv"))
    ap.add_argument("--check", action="store_true", help="validate only, write nothing")
    args = ap.parse_args()
    df = build()
    n_exc = sum(len(v) for v in EXCLUDED.values())
    print(f"{len(df)} ranked pairs across {df['newsletter'].nunique()} newsletters "
          f"({n_exc} excluded by review)")
    counts = df.groupby("tier").size()
    for t in sorted(counts.index):
        print(f"  {t} {TIER_NAME[t]:<11}{counts[t]:>4}")
    if args.check:
        print("\nvalidated against the crosswalk; nothing written")
        return 0
    df.to_csv(args.out, index=False, lineterminator="\n")
    print(f"\nwritten -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
