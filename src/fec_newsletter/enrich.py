"""Build enhanced_crp_l_crosswalk.csv — LLM-inferred coverage for committees CRP misses.

2,872 committees that actually move money in the pipelines resolve to no catcode at all
($434M on the send side, $691M on the receive side), and ~60 catcodes that committees do
carry are absent from crp_l_crosswalk. Those dollars are dropped today. This module asks
an Azure OpenAI deployment to classify the gap from committee names and metadata, and
writes the result as a crosswalk the pipelines can layer on top of CRP's.

Design constraints, in order of importance:

1. **"No newsletter" must be an available answer, and it is the default.** Most unmapped
   committees are party, candidate, or leadership PACs that CRP deliberately leaves outside
   the industry newsletters. A classifier that always picks a newsletter would manufacture
   attribution — the exact failure this pipeline exists to avoid. The schema makes
   `newsletter: null` a first-class outcome and the prompt names it as the default.
2. **The output vocabulary is closed.** `newsletter`, `sector`, and `industry` are enums
   built from the live crosswalk, so a hallucinated category cannot enter the join.
3. **Every inferred row is auditable.** Confidence, one-line reasoning, the evidence used,
   and the model that produced it all ship in the CSV. Nothing is inferred silently.
4. **CRP always wins.** Real crosswalk rows pass through untouched; inferences only fill
   committees CRP has nothing for.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field

import pandas as pd

from . import common, config

# Azure OpenAI. Unlike the plain OpenAI API, the `model` argument is the **deployment
# name** from your Azure resource, not a model name — they often coincide, but the
# deployment is what the API resolves. Configured in .env as AZURE_OPENAI_DEPLOYMENT_CHAT.
#
# The deployment must be backed by a model that supports Structured Outputs, and the
# api-version must be 2024-08-01-preview or later: the closed vocabulary below is enforced
# by strict JSON-schema mode, and without it the model can return categories that don't
# exist and quietly corrupt the join.
DEFAULT_DEPLOYMENT = "gpt-4.1"
DEFAULT_API_VERSION = "2024-12-01-preview"

# Committees per request. Small enough that one bad batch is cheap to redo, large enough
# that the shared instructions amortise across many classifications.
BATCH_SIZE = 25

# Confidence below this is written to the CSV but excluded from the join by default, so a
# guess never silently becomes an attribution. Defined in config because common.py applies
# the same floor when the send pipeline reads the enhanced crosswalk.
MIN_CONFIDENCE = config.MIN_CROSSWALK_CONFIDENCE
CONFIDENCE_ORDER = config.CONFIDENCE_ORDER

SYSTEM_PROMPT = """\
You classify US political committees (PACs) into industry categories for a campaign-finance \
newsletter, matching the scheme the Center for Responsive Politics (CRP/OpenSecrets) uses.

For each committee you are given its FEC ID, name, and whatever metadata the FEC files \
carry: committee type, organization type, connected organization, and names seen on its \
transactions. Infer the industry the committee represents.

The single most important rule: **most committees do not belong to any newsletter.** Party \
committees, candidate committees, leadership PACs, joint fundraising committees, ideological \
and single-issue groups without an industry basis, and generic political organizations all \
get `newsletter: null`. CRP deliberately places these outside the industry newsletters. \
Returning null is the correct, expected answer and is never penalised — assigning a \
newsletter to a party or candidate committee corrupts the dataset it feeds.

Only assign a newsletter when the committee's name or connected organization identifies a \
specific commercial industry, trade association, or industrial labor union. A committee \
sponsored by a named company, trade body, or sector union belongs to that sector's \
newsletter. If the name is opaque, generic, or you are guessing, set `newsletter: null` and \
`confidence: "low"`.

Assign `sector` and `industry` whenever you can, even where `newsletter` is null — a party \
committee still has sector "Party Cmte". Use them to record what the committee *is*, and \
reserve `newsletter` for genuine industry attribution.

Set `confidence`:
  high   — the name or connected organization names a company, trade association, or union
           whose industry is unambiguous.
  medium — the industry is clear from context but the mapping to a category involves judgment.
  low    — you are guessing. Pair with `newsletter: null`.

Give `reasoning` as one short sentence citing the specific evidence you used."""


@dataclass
class Vocabulary:
    """Closed category sets, read from the live crosswalk so they cannot drift."""

    newsletters: list[str] = field(default_factory=list)
    sectors: list[str] = field(default_factory=list)
    industries: list[str] = field(default_factory=list)

    @classmethod
    def from_crosswalk(cls, crosswalk: pd.DataFrame) -> "Vocabulary":
        def atoms(col: str, split: bool) -> list[str]:
            vals: set[str] = set()
            for raw in crosswalk[col].fillna(""):
                parts = raw.split(",") if split else [raw]
                vals.update(p.strip() for p in parts if p.strip())
            return sorted(vals)

        return cls(
            # Split the comma-separated multi-newsletter strings into atoms: the model
            # picks from single newsletters, and multi-assignment is expressed by
            # returning several, not by inventing a new combined string.
            newsletters=atoms("newsletter", split=True),
            sectors=atoms("sector", split=False),
            industries=atoms("industry", split=False),
        )

    def schema(self) -> dict:
        return {
            "type": "object",
            "properties": {
                "classifications": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "cmte_id": {"type": "string"},
                            "newsletters": {
                                "type": "array",
                                "items": {"type": "string", "enum": self.newsletters},
                                "description": (
                                    "Empty array when the committee belongs to no industry "
                                    "newsletter. This is the common, expected case."
                                ),
                            },
                            # Nullable enums use a union `type` array with null included
                            # in `enum` — the documented shape for OpenAI strict mode.
                            "sector": {
                                "type": ["string", "null"],
                                "enum": self.sectors + [None],
                            },
                            "industry": {
                                "type": ["string", "null"],
                                "enum": self.industries + [None],
                            },
                            "confidence": {
                                "type": "string",
                                "enum": ["high", "medium", "low"],
                            },
                            "reasoning": {"type": "string"},
                        },
                        "required": [
                            "cmte_id", "newsletters", "sector", "industry",
                            "confidence", "reasoning",
                        ],
                        "additionalProperties": False,
                    },
                }
                },
            "required": ["classifications"],
            "additionalProperties": False,
        }


def counterparty_names(oth: pd.DataFrame, pas2: pd.DataFrame) -> pd.Series:
    """OTHER_ID -> NAME, from the raw source tables.

    In both files NAME is the *counterparty's* name, so this is often the only name we have
    for a committee: 1,480 of the unresolved committees are absent from
    fec_committees_trim_mapped entirely and would otherwise be unclassifiable.
    """
    frames = [
        df[["OTHER_ID", "NAME"]]
        for df in (oth, pas2)
        if {"OTHER_ID", "NAME"} <= set(df.columns)
    ]
    if not frames:
        return pd.Series(dtype=str)
    names = pd.concat(frames, ignore_index=True)
    names = names[(names["OTHER_ID"].fillna("") != "") & (names["NAME"].fillna("") != "")]
    return names.drop_duplicates("OTHER_ID").set_index("OTHER_ID")["NAME"]


def unresolved_committees(
    transactions: pd.DataFrame,
    committees: pd.DataFrame,
    crp_mapping: pd.DataFrame,
    crosswalk: pd.DataFrame,
    extra_names: pd.Series | None = None,
) -> pd.DataFrame:
    """Committees that move money in the pipelines but resolve to no crosswalk row.

    Restricted to committees that actually appear as a sender or recipient — classifying
    all 64,805 committees would cost far more and add nothing, since the rest never carry a
    dollar through either pipeline.
    """
    cm = committees.copy()
    cm["catcode"] = common.norm_catcode(cm["catcode"])
    crp = crp_mapping.copy()
    crp["catcode"] = common.norm_catcode(crp["catcode"])

    primary = cm[cm["catcode"] != ""].set_index("CMTE_ID")["catcode"]
    fallback = crp[crp["catcode"] != ""].set_index("cmte_id")["catcode"]
    known_catcodes = set(common.norm_catcode(crosswalk["catcode"]))

    seen = pd.concat([transactions["sender_id"], transactions["recip_id"]]).dropna()
    seen = pd.Series(sorted(seen[seen.str[:1] == "C"].unique()), name="cmte_id")

    catcode = seen.map(primary).fillna(seen.map(fallback))
    unresolved = seen[catcode.isna() | ~catcode.isin(known_catcodes)]

    meta = cm.set_index("CMTE_ID")
    txn_names = extra_names if extra_names is not None else pd.Series(dtype=str)

    out = pd.DataFrame({"cmte_id": unresolved.reset_index(drop=True)})
    for col, src in [
        ("cmte_name", "CMTE_NM"), ("cmte_type", "CMTE_TP"),
        ("cmte_designation", "CMTE_DSGN"), ("connected_org", "CONNECTED_ORG_NM"),
        ("org_type", "ORG_TP"),
    ]:
        out[col] = out["cmte_id"].map(meta[src]) if src in meta else pd.NA
    out["txn_name"] = out["cmte_id"].map(txn_names)
    out["cmte_name"] = out["cmte_name"].fillna(out["txn_name"])
    return out.reset_index(drop=True)


def _describe(row: pd.Series) -> dict:
    """Only non-empty fields — blank metadata is noise the model would try to interpret."""
    fields = {
        "cmte_id": row["cmte_id"],
        "name": row.get("cmte_name"),
        "committee_type": row.get("cmte_type"),
        "designation": row.get("cmte_designation"),
        "connected_organization": row.get("connected_org"),
        "organization_type": row.get("org_type"),
    }
    return {k: v for k, v in fields.items() if pd.notna(v) and str(v).strip()}


class TransientError(RuntimeError):
    """An API failure worth retrying -- rate limit, timeout, 5xx.

    Separated from a bad *answer* (a refusal, or JSON that does not parse) because only
    one of the two is worth spending another request on.
    """


def classify_batch(client, batch: pd.DataFrame, vocab: Vocabulary, deployment: str) -> list[dict]:
    """Classify one batch.

    Raises TransientError if the call itself failed and is worth retrying; returns [] if
    the model answered but the answer was unusable (refusal, unparseable JSON), which
    retrying would not fix.

    `strict: True` is what makes the closed vocabulary a guarantee rather than a request —
    without it the model can return a category outside the enum and silently corrupt the
    join.
    """
    payload = [_describe(r) for _, r in batch.iterrows()]
    try:
        response = client.chat.completions.create(
            model=deployment,          # Azure: the DEPLOYMENT name, not a model name
            max_tokens=16000,
            temperature=0,             # classification, not generation
            response_format={
                "type": "json_schema",
                "json_schema": {
                    "name": "committee_classifications",
                    "strict": True,
                    "schema": vocab.schema(),
            },
            },
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": "Classify these committees:\n\n" + json.dumps(payload, indent=1),
                },
            ],
        )
    except Exception as exc:                      # noqa: BLE001 - re-raised as typed
        raise TransientError(str(exc)) from exc
    choice = response.choices[0]
    if getattr(choice.message, "refusal", None):
        return []
    try:
        return json.loads(choice.message.content)["classifications"]
    except (json.JSONDecodeError, KeyError, TypeError):
        return []


def load_checkpoint(path) -> tuple[list[dict], set[str]]:
    """Read a partial run back. Returns (classifications, committee ids already done).

    One JSON object per line, appended and flushed as each batch lands, so an interrupted
    run loses at most the batch in flight rather than everything before it.
    """
    if not path.exists():
        return [], set()
    rows = []
    with path.open() as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue                          # a torn final line from a hard kill
    return rows, {r["cmte_id"] for r in rows if r.get("cmte_id")}


def append_checkpoint(path, rows: list[dict]) -> None:
    """Append a batch and flush to disk before the next request is made."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")
        fh.flush()
        os.fsync(fh.fileno())


def build_client() -> tuple[object, str]:
    """Azure OpenAI client, keyed from the same .env as the database credentials.

    Returns (client, deployment) — the deployment name is a separate argument on every
    call, so it travels with the client rather than being read again at each call site.
    """
    from dotenv import load_dotenv

    load_dotenv(config.REPO_ROOT / ".env")
    missing = [k for k in ("AZURE_OPENAI_ENDPOINT", "AZURE_OPENAI_KEY") if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"Missing required env var(s): {', '.join(missing)}. Add them to .env "
            f"alongside the Postgres credentials."
        )
    from openai import AzureOpenAI

    client = AzureOpenAI(
        azure_endpoint=os.environ["AZURE_OPENAI_ENDPOINT"],
        api_key=os.environ["AZURE_OPENAI_KEY"],
        api_version=os.getenv("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION),
    )
    return client, os.getenv("AZURE_OPENAI_DEPLOYMENT_CHAT", DEFAULT_DEPLOYMENT)


def to_crosswalk_rows(
    classifications: list[dict], candidates: pd.DataFrame, model: str = DEFAULT_DEPLOYMENT
) -> pd.DataFrame:
    """Shape classifications like crosswalk rows, keyed by committee rather than catcode."""
    names = candidates.set_index("cmte_id")["cmte_name"]
    rows = []
    for c in classifications:
        newsletters = [n for n in (c.get("newsletters") or []) if n]
        rows.append({
            "catcode": pd.NA,                       # inferred rows are committee-keyed
            "cmte_id": c["cmte_id"],
            "catorder": pd.NA,
            "catname": names.get(c["cmte_id"], pd.NA),
            "industry": c.get("industry") or "",
            "sector": c.get("sector") or "",
            "newsletter": ", ".join(newsletters),   # matches CRP's comma-joined convention
            "source": "llm_inferred",
            "confidence": c.get("confidence", "low"),
            "reasoning": c.get("reasoning", ""),
            "model": model,
        })
    return pd.DataFrame(rows, columns=ENHANCED_COLUMNS)


ENHANCED_COLUMNS = [
    "catcode", "cmte_id", "catorder", "catname",
    "industry", "sector", "newsletter",
    "source", "confidence", "reasoning", "model",
]


def merge(crosswalk: pd.DataFrame, inferred: pd.DataFrame) -> pd.DataFrame:
    """CRP rows first and unchanged; inferred committee rows appended below them."""
    base = crosswalk.copy()
    base["cmte_id"] = pd.NA
    base["source"] = "crp"
    base["confidence"] = "high"      # CRP's own mapping is the reference, by definition
    base["reasoning"] = ""
    base["model"] = ""
    return pd.concat([base[ENHANCED_COLUMNS], inferred], ignore_index=True)


def usable(enhanced: pd.DataFrame, min_confidence: str = MIN_CONFIDENCE) -> pd.DataFrame:
    """Rows trustworthy enough to attribute money with.

    Inferred rows below the confidence floor stay in the CSV for review but are excluded
    here, so a low-confidence guess never quietly becomes a newsletter total.
    """
    floor = CONFIDENCE_ORDER[min_confidence]
    keep = (enhanced["source"] == "crp") | (
        enhanced["confidence"].map(CONFIDENCE_ORDER).fillna(-1) >= floor
    )
    return enhanced[keep & (enhanced["newsletter"].fillna("").str.strip() != "")]
