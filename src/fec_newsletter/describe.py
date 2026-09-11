"""One-sentence explanations of who a PAC is and what a pairing means.

The last column of every newsletter table used to carry a mechanical fact
("190 recipients, none above 1%"). This module replaces it with an explanation of
the entities and the relationship -- "Fairshake and Protect Progress are both
crypto-industry super PACs, and this is money moving between two arms of the same
operation" -- which is the thing a reader actually needs to interpret the row.

The whole design problem here is hallucination. These sentences ship in a
published newsletter, so the module is built to make invention hard:

1. **Everything the model can lean on is passed in as facts.** Committee type,
   designation, connected organization and CRP industry come from the FEC files;
   concentration and both-sides giving are computed from the transactions. The
   Fairshake/Protect Progress affiliation, for instance, is in the filings
   (CONNECTED_ORG_NM points each at the other) -- the model is confirming
   FEC-disclosed structure, not recalling it.
2. **Unknown is a first-class answer.** A committee absent from the FEC master
   file has almost nothing to describe; the schema and prompt both require the
   model to say so rather than guess a plausible industry.
3. **Every sentence is cached with its grounding and a flag saying whether it
   relied on outside knowledge**, so a human can review exactly what was asserted
   and on what basis before it goes out.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pandas as pd

from . import config
from .enrich import DEFAULT_DEPLOYMENT, TransientError

CACHE = config.REPO_ROOT / "cache" / "pac_descriptions.json"

# FEC committee type codes, spelled out. The distinction that matters most to a
# reader is super PAC (unlimited money, no coordination) vs a traditional PAC
# (capped contributions straight to campaigns).
CMTE_TYPE = {
    "O": "independent-expenditure-only committee (super PAC)",
    "V": "hybrid PAC with a non-contribution account",
    "W": "hybrid PAC with a non-contribution account",
    "Q": "qualified multi-candidate PAC",
    "N": "non-qualified PAC",
    "Y": "qualified party committee",
    "X": "non-qualified party committee",
    "H": "House campaign committee",
    "S": "Senate campaign committee",
    "P": "presidential campaign committee",
    "D": "delegate committee",
    "I": "independent-expenditure filer",
    "U": "single-candidate independent-expenditure committee",
}

SYSTEM_PROMPT = """\
You write one-sentence explanations for a US campaign-finance newsletter, telling \
readers who a political committee is and what a transaction between two committees \
means.

You are given FEC filing facts for each committee: its registered name, FEC \
committee type, connected organization, CRP industry classification, and how it \
behaved this quarter. Those facts are authoritative -- prefer them over anything \
you recall.

SOURCES
You have a web_search tool. Use it for any committee whose identity is not already \
clear from the filing facts -- an unfamiliar name, a CRP code that tells you nothing, a \
sponsor you cannot place. Do not search when the facts already answer the question.

When you do search, put the URLs you actually relied on in `sources` and set \
`uses_outside_knowledge` true. When you do not, leave `sources` empty. Never put a URL \
in `sources` that you did not read.

VOICE
Write like a political reporter, not like a compliance analyst. The model for \
these sentences is a newspaper's campaign-finance desk: plain, active, specific, \
and unimpressed. One sentence, under 30 words.

- Lead with the actor and give it a real verb. "Fairshake moved $19M to ..." \
  not "A transfer was made by Fairshake to ...".
- Identify committees the way a newspaper does on first reference: an appositive \
  in plain English. "Fairshake, the crypto industry's main super PAC," / "BDA PAC, \
  the bond dealers' political arm," / "Realtors PAC, the real-estate agents' \
  national committee,".
- Translate the filing vocabulary instead of quoting it. Readers do not know what \
  a "hybrid PAC classified as general ideological" is. Say what it does: a super \
  PAC that also gives directly to candidates; an industry trade group's PAC; a \
  single-candidate super PAC.
- NEVER print a CRP classification phrase as if it described the committee. \
  "general ideological", "general business associations", "leadership committee" \
  are codes in a lookup table, not English. If you know what the committee actually \
  is, say that; if you do not, say what its filings do and do not disclose. A \
  committee coded "general ideological" that you recognise as an industry vehicle is \
  the industry vehicle -- flag `uses_outside_knowledge` and name it.
- Render names in ordinary capitalisation, not the all-caps of the filings. \
  "PricewaterhouseCoopers PAC", never "PRICEWATERHOUSECOOPERS POLITICAL ACTION \
  COMMITTEE I".
- Cut hedges and bureaucratic padding. "was the largest single outside supporter \
  of" becomes "was the biggest outside backer of". No "it should be noted", no \
  "as part of its quarterly activity". Say "this period" or name the period given
  in the facts -- an edition may cover a month, and calling it a quarter is wrong.
- The most useful sentences explain a structure the number alone does not reveal: \
  that two committees are arms of the same operation, that a state trade group is \
  routing money to its national arm, that a super PAC is spending on a race rather \
  than giving to a campaign. Reach for that before reaching for a classification.

Reporting plainly is not the same as insinuating. A newspaper says who gave what \
to whom and what the two parties are; it does not tell the reader that a seat was \
bought. Keep the edge in the specificity, never in the adjectives.

Where you genuinely recognise a committee, name what it actually is even if the \
filings are thinner or out of date -- CRP's industry codes lag new sectors, so a \
committee coded "general ideological" may in fact be the well-known vehicle of a \
specific industry. Saying so is the most valuable thing you can add, PROVIDED you \
set `uses_outside_knowledge` to true so a human reviews it.

ACCURACY RULES -- these outrank the voice guidance in every conflict:
- Attribute an action ONLY to the committee the facts name as its actor. Facts \
about a member -- money spent against them, how many backers they have -- describe \
the member's situation, NOT the behaviour of the committee you are writing about. \
Never write that a backer opposed someone unless a fact explicitly says that backer \
did so.
- Never invent an industry, sponsor or affiliation. Recognising a prominent \
committee is fine; guessing from the shape of a name is not. A punchier sentence is \
never worth a fact you cannot source.
- When a committee is absent from the FEC master file, say that its filings carry \
no sponsor rather than guessing what it might be. "A committee that discloses no \
sponsor" is a perfectly good newspaper sentence.
- Prefer the STRUCTURE the facts reveal over restating a classification code. If a \
sender's whole quarter went to two or three named committees, that network is the \
story and the CRP code on any one of them is not.
- A subcommittee name is a committee seat, not the name of a newsletter channel; \
do not call it a "channel".
- Do not restate what the row already shows: not the dollar amount, and not a \
member's party and state, both of which sit in their own columns.
- Do not editorialise about whether the money is good or bad, and do not imply \
anything was bought.
- Set `uses_outside_knowledge` to true if any part of your sentence relies on \
something not present in the facts given. This is allowed and often useful, but \
it must be declared so a human can check it."""

# A sentence is only reusable while the instructions that produced it still stand.
# Cached entries carry the fingerprint of the prompt that wrote them, so editing
# SYSTEM_PROMPT invalidates them automatically -- otherwise a voice change is a
# silent no-op, every sentence being served from a cache keyed on the committee alone.
PROMPT_FINGERPRINT = hashlib.sha256(SYSTEM_PROMPT.encode()).hexdigest()[:12]


SCHEMA = {
    "type": "object",
    "properties": {
        "descriptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "sentence": {"type": "string"},
                    "uses_outside_knowledge": {"type": "boolean"},
                    # Under a strict JSON schema the API returns NO url_citation
                    # annotations -- they attach to free text, and there is none. So the
                    # model reports what it consulted here instead. Self-reported, and
                    # therefore a lead to check rather than proof, which is why the
                    # edition prints them as sources rather than as footnotes.
                    "sources": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["key", "sentence", "uses_outside_knowledge", "sources"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["descriptions"],
    "additionalProperties": False,
}


def committee_facts(cmte_id: str, committees: pd.DataFrame,
                    crosswalk: pd.DataFrame) -> dict:
    """Everything the FEC files say about one committee, decoded."""
    row = committees[committees["CMTE_ID"] == cmte_id]
    if row.empty:
        return {"fec_id": cmte_id,
                "note": "absent from the FEC committee master file; no sponsor or "
                        "industry on record"}
    r = row.iloc[0]
    out = {"name": r.get("CMTE_NM"), "fec_id": cmte_id}
    tp = (r.get("CMTE_TP") or "").strip()
    if tp:
        out["committee_type"] = CMTE_TYPE.get(tp, f"FEC type {tp}")
    org = (r.get("CONNECTED_ORG_NM") or "").strip()
    if org and org.upper() != "NONE":
        out["connected_organization"] = org
    cat = (r.get("catcode") or "").strip().upper()
    if cat:
        xw = crosswalk[crosswalk["catcode"].str.upper().str.strip() == cat]
        if not xw.empty:
            out["crp_industry"] = xw.iloc[0].get("catname")
            out["crp_sector"] = xw.iloc[0].get("sector")
    return {k: v for k, v in out.items() if v}


def affiliates(cmte_id: str, committees: pd.DataFrame) -> list[str]:
    """Committees that name this one as their connected organisation, and vice versa.

    CONNECTED_ORG_NM points both ways for an affiliated pair -- Fairshake names
    Protect Progress and Protect Progress names Fairshake -- so a reverse lookup
    turns a single field into a disclosed network edge.
    """
    row = committees[committees["CMTE_ID"] == cmte_id]
    if row.empty:
        return []
    name = (row.iloc[0].get("CMTE_NM") or "").strip().upper()
    org = (row.iloc[0].get("CONNECTED_ORG_NM") or "").strip()
    found = set()
    if org and org.upper() != "NONE":
        found.add(org)
    if name:
        back = committees[committees["CONNECTED_ORG_NM"].fillna("").str.strip().str.upper() == name]
        found.update(n for n in back["CMTE_NM"].dropna() if n.strip().upper() != name)
    return sorted(found)


def pair_facts(sender_id: str, recip_id: str, committees: pd.DataFrame,
               crosswalk: pd.DataFrame, behaviour: dict | None = None) -> dict:
    facts = {"sender": committee_facts(sender_id, committees, crosswalk),
             "recipient": committee_facts(recip_id, committees, crosswalk)}
    for side, cid in (("sender", sender_id), ("recipient", recip_id)):
        aff = affiliates(cid, committees)
        if aff:
            facts[side]["affiliated_committees_per_filings"] = aff
    if behaviour:
        facts["this_period"] = behaviour
    return facts


def load_cache() -> dict:
    if CACHE.exists():
        try:
            return json.loads(CACHE.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def save_cache(cache: dict) -> None:
    CACHE.parent.mkdir(parents=True, exist_ok=True)
    CACHE.write_text(json.dumps(cache, indent=1, sort_keys=True))


def build_client() -> tuple[object, str]:
    from dotenv import load_dotenv
    load_dotenv(config.REPO_ROOT / ".env")
    missing = [k for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_KEY") if not os.getenv(k)]
    if missing:
        raise RuntimeError(f"Missing env var(s): {', '.join(missing)}")
    # The Responses API, not Chat Completions: web_search is only offered there. It is
    # reached through Azure's /openai/v1/ endpoint with the plain OpenAI client, which
    # carries no api_version -- the versioned AzureOpenAI client cannot see the tool.
    from openai import OpenAI
    client = OpenAI(
        base_url=os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/") + "/openai/v1/",
        api_key=os.environ["AZURE_OPENAI_KEY"],
    )
    return client, os.getenv("AZURE_OPENAI_DEPLOYMENT_CHAT", DEFAULT_DEPLOYMENT)


def describe(items: list[dict], client=None, deployment: str | None = None,
             use_cache: bool = True) -> dict[str, dict]:
    """items: [{"key": str, "facts": dict}] -> {key: {sentence, uses_outside_knowledge}}.

    Cached by key, because a committee's identity does not change quarter to
    quarter -- only the first edition that mentions a PAC pays for it.
    """
    cache = load_cache() if use_cache else {}
    fresh = lambda k: (k in cache
                       and cache[k].get("prompt") == PROMPT_FINGERPRINT
                       and cache[k].get("sentence"))
    todo = [it for it in items if not fresh(it["key"])]
    stale = sum(1 for it in items if it["key"] in cache and not fresh(it["key"]))
    if stale:
        print(f"  [describe] {stale} cached sentence(s) predate the current prompt "
              f"({PROMPT_FINGERPRINT}) and will be rewritten")
    if not todo:
        return {it["key"]: cache[it["key"]] for it in items}

    if client is None:
        client, deployment = build_client()
    payload = [{"key": it["key"], "facts": it["facts"]} for it in todo]
    try:
        resp = client.responses.create(
            model=deployment,
            max_output_tokens=8000,
            temperature=0.2,
            tools=[{"type": "web_search"}],
            # "auto" rather than forcing the tool: a batch of committees the filings
            # already explain should not pay for a Bing call. The system prompt is what
            # makes the model reach for it -- with no instruction it answers from memory
            # and never searches, which is the failure this is meant to fix.
            tool_choice="auto",
            text={"format": {"type": "json_schema", "name": "pac_descriptions",
                             "strict": True, "schema": SCHEMA}},
            input=[{"role": "system", "content": SYSTEM_PROMPT},
                   {"role": "user",
                    "content": "Explain each of these:\n\n" + json.dumps(payload, indent=1)}],
        )
    except Exception as exc:                              # noqa: BLE001
        raise TransientError(str(exc)) from exc

    body = resp.output_text
    if not body:                       # refusal, or output that never reached a message
        return {it["key"]: cache.get(it["key"], {}) for it in items}
    searched = sum(1 for it in resp.output if it.type == "web_search_call")
    for d in json.loads(body)["descriptions"]:
        cache[d["key"]] = {"sentence": d["sentence"],
                           "uses_outside_knowledge": d["uses_outside_knowledge"],
                           "sources": [u for u in d.get("sources", []) if u],
                           "model": deployment,
                           "prompt": PROMPT_FINGERPRINT}
    if searched:
        print(f"  [describe] {searched} web search(es) for {len(payload)} committee(s)")
    if use_cache:
        save_cache(cache)
    return {it["key"]: cache.get(it["key"], {}) for it in items}
