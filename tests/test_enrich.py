"""Tests for the crosswalk enrichment module.

None of these call the Anthropic API — they cover the parts that decide whether an LLM
answer is safe to join against money: the closed vocabulary, the CRP-wins merge, and the
confidence gate.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fec_newsletter import config, enrich, extract  # noqa: E402


@pytest.fixture(scope="module")
def crosswalk():
    path = config.CACHE_DIR / "crosswalk.csv"
    if not path.exists():
        pytest.skip("no cached crosswalk; run `python run_send.py` first")
    return extract.load({"crosswalk": path})["crosswalk"]


@pytest.fixture(scope="module")
def vocab(crosswalk):
    return enrich.Vocabulary.from_crosswalk(crosswalk)


# --- Vocabulary is closed and derived from live data -----------------------------------

def test_newsletters_are_atomic(vocab):
    """The model picks single newsletters; multi-assignment is a list, not a new string.

    Offering the combined 'Social Issues, Healthcare' strings as enum values would let the
    model invent combinations that don't exist in CRP's scheme.
    """
    assert vocab.newsletters
    assert not any("," in n for n in vocab.newsletters)


def test_vocabulary_matches_the_live_crosswalk(vocab, crosswalk):
    """Categories come from the data, so they can't drift from what the join expects."""
    atoms = {p.strip() for v in crosswalk["newsletter"].fillna("") for p in v.split(",") if p.strip()}
    assert set(vocab.newsletters) == atoms
    assert set(vocab.sectors) == {s.strip() for s in crosswalk["sector"].fillna("") if s.strip()}


def test_nullable_enums_use_the_openai_strict_shape(vocab):
    """OpenAI strict mode expresses nullable as a union `type` array with null in `enum`."""
    props = vocab.schema()["properties"]["classifications"]["items"]["properties"]
    for field in ("sector", "industry"):
        assert props[field]["type"] == ["string", "null"]
        assert None in props[field]["enum"]


def test_schema_forbids_extra_properties(vocab):
    """Strict mode requires additionalProperties: false on every object."""
    schema = vocab.schema()
    assert schema["additionalProperties"] is False
    assert schema["properties"]["classifications"]["items"]["additionalProperties"] is False


def test_every_property_is_required(vocab):
    """Strict mode rejects a schema where any property is omitted from `required`."""
    items = vocab.schema()["properties"]["classifications"]["items"]
    assert set(items["required"]) == set(items["properties"])
    root = vocab.schema()
    assert set(root["required"]) == set(root["properties"])


def test_no_newsletter_is_expressible(vocab):
    """The empty array must be a legal answer — it is the expected one for party PACs."""
    items = vocab.schema()["properties"]["classifications"]["items"]
    assert "minItems" not in items["properties"]["newsletters"]
    assert "newsletters" in items["required"]


def test_prompt_makes_null_the_default():
    """Guards the single most important instruction against a careless prompt edit."""
    prompt = enrich.SYSTEM_PROMPT.lower()
    assert "most committees do not belong to any newsletter" in prompt
    assert "null" in prompt


# --- Merge: CRP always wins -------------------------------------------------------------

def _inferred(**kw):
    row = {
        "catcode": pd.NA, "cmte_id": "C00000001", "catorder": pd.NA, "catname": "TEST PAC",
        "industry": "", "sector": "", "newsletter": "Healthcare",
        "source": "llm_inferred", "confidence": "high", "reasoning": "test", "model": "m",
    }
    row.update(kw)
    return pd.DataFrame([row], columns=enrich.ENHANCED_COLUMNS)


def test_merge_preserves_every_crp_row(crosswalk):
    merged = enrich.merge(crosswalk, _inferred())
    crp = merged[merged["source"] == "crp"]
    assert len(crp) == len(crosswalk)
    assert set(crp["catcode"]) == set(crosswalk["catcode"])


def test_merge_does_not_alter_crp_newsletters(crosswalk):
    """Inference fills gaps; it never edits CRP's own mapping."""
    merged = enrich.merge(crosswalk, _inferred())
    crp = merged[merged["source"] == "crp"].set_index("catcode")["newsletter"]
    original = crosswalk.set_index("catcode")["newsletter"]
    assert crp.reindex(original.index).fillna("").tolist() == original.fillna("").tolist()


def test_inferred_rows_are_committee_keyed(crosswalk):
    merged = enrich.merge(crosswalk, _inferred())
    inferred = merged[merged["source"] == "llm_inferred"]
    assert inferred["cmte_id"].notna().all()
    assert inferred["catcode"].isna().all()


# --- Confidence gate --------------------------------------------------------------------

def test_low_confidence_is_excluded_from_usable(crosswalk):
    """A guess must not silently become a newsletter attribution."""
    merged = enrich.merge(crosswalk, _inferred(confidence="low"))
    assert "C00000001" not in set(enrich.usable(merged)["cmte_id"].dropna())


def test_high_confidence_is_included(crosswalk):
    merged = enrich.merge(crosswalk, _inferred(confidence="high"))
    assert "C00000001" in set(enrich.usable(merged)["cmte_id"].dropna())


def test_blank_newsletter_never_reaches_usable(crosswalk):
    """A committee classified as belonging to no newsletter must not join to one."""
    merged = enrich.merge(crosswalk, _inferred(newsletter="", confidence="high"))
    assert "C00000001" not in set(enrich.usable(merged)["cmte_id"].dropna())


def test_usable_keeps_only_rows_with_a_newsletter(crosswalk):
    result = enrich.usable(enrich.merge(crosswalk, _inferred()))
    assert (result["newsletter"].str.strip() != "").all()


# --- Row shaping ------------------------------------------------------------------------

def test_multiple_newsletters_join_with_crp_convention():
    """Must match CRP's comma-joined format so the existing fan-out handles it unchanged."""
    candidates = pd.DataFrame([{"cmte_id": "C1", "cmte_name": "X"}])
    rows = enrich.to_crosswalk_rows(
        [{"cmte_id": "C1", "newsletters": ["Healthcare", "Social Issues"],
          "sector": None, "industry": None, "confidence": "high", "reasoning": "r"}],
        candidates,
    )
    assert rows.loc[0, "newsletter"] == "Healthcare, Social Issues"


def test_empty_newsletter_list_becomes_blank():
    candidates = pd.DataFrame([{"cmte_id": "C1", "cmte_name": "X"}])
    rows = enrich.to_crosswalk_rows(
        [{"cmte_id": "C1", "newsletters": [], "sector": "Party Cmte",
          "industry": None, "confidence": "high", "reasoning": "r"}],
        candidates,
    )
    assert rows.loc[0, "newsletter"] == ""
    assert rows.loc[0, "sector"] == "Party Cmte"


# --- Candidate selection ----------------------------------------------------------------

def test_only_unresolved_committees_are_candidates(crosswalk):
    """Committees CRP already covers must never be sent to the model."""
    committees = pd.DataFrame([
        {"CMTE_ID": "C1", "CMTE_NM": "MAPPED", "catcode": "H4300",
         "CMTE_TP": "", "CMTE_DSGN": "", "CONNECTED_ORG_NM": "", "ORG_TP": ""},
        {"CMTE_ID": "C2", "CMTE_NM": "UNMAPPED", "catcode": "",
         "CMTE_TP": "", "CMTE_DSGN": "", "CONNECTED_ORG_NM": "", "ORG_TP": ""},
    ])
    txns = pd.DataFrame({"sender_id": ["C1", "C2"], "recip_id": ["C2", "C1"]})
    crp_mapping = pd.DataFrame(columns=["cmte_id", "catcode"])

    result = enrich.unresolved_committees(txns, committees, crp_mapping, crosswalk)
    assert set(result["cmte_id"]) == {"C2"}


def test_counterparty_names_fill_missing_committee_names():
    """1,480 unresolved committees are absent from the committee table entirely."""
    oth = pd.DataFrame({"OTHER_ID": ["C9"], "NAME": ["ACME TRADE ASSOCIATION PAC"]})
    pas2 = pd.DataFrame({"OTHER_ID": [], "NAME": []})
    names = enrich.counterparty_names(oth, pas2)
    assert names["C9"] == "ACME TRADE ASSOCIATION PAC"
