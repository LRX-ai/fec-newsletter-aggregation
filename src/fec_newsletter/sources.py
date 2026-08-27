"""Normalise the two FEC source tables into one common transaction shape.

Both tables describe "committee X moved money towards Y", but disagree about almost every
detail of how they say it: which column is the payer, what OTHER_ID means, what date format
is used, and which transaction types appear. Each adapter here absorbs those differences so
the send and receive pipelines can work on a single shape:

    source          'oth' | 'pas2'
    sub_id          FEC's unique row ID -- SHARED ACROSS TABLES, so it is the exact key for
                    cross-source dedupe
    sender_id       who paid
    recip_id        who was paid, AS FILED (a committee for committee-to-committee money,
                    the candidate's authorized committee for PAS2 contributions)
    recip_cand_id   the candidate, where the source names one (PAS2 only)
    transaction_dt  ISO YYYY-MM-DD, normalised from each table's own format
    amount          numeric
    flow_type       what KIND of money this is -- never summed across
    support_oppose  'support' | 'oppose'

`recip_id` is deliberately the ID as filed rather than the candidate, because it is what
makes the fingerprint of a transaction line up across the two files and across both sides
of an OTH double-report.
"""

from __future__ import annotations

import pandas as pd

from . import config
from .common import Coverage

NORMALISED_COLUMNS = [
    "source", "sub_id", "sender_id", "recip_id", "recip_cand_id",
    "transaction_dt", "amount", "flow_type", "support_oppose",
    "transaction_tp", "transaction_pgi", "amndt_ind", "memo_cd", "cycle",
]


def _finish(df: pd.DataFrame) -> pd.DataFrame:
    for col in NORMALISED_COLUMNS:
        if col not in df:
            df[col] = pd.NA
    return df[NORMALISED_COLUMNS]


def from_oth(txns: pd.DataFrame, cov: Coverage) -> pd.DataFrame:
    """usa.fec_pac_to_pac -- direction depends on TRANSACTION_TP.

    Includes the candidate-directed types (24A/24C/24E/24F/24N) that the receive pipeline
    cannot use. They are harmless here: the send pipeline attributes from the sender, and
    the cross-source dedupe removes the ~98% of them that PAS2 also reports.
    """
    df = txns.copy()
    df["amount"] = pd.to_numeric(df["TRANSACTION_AMT"], errors="coerce")
    cov.record("OTH extracted (non-15J)", df)

    bad = int(df["amount"].isna().sum())
    if bad:
        raise ValueError(f"{bad} OTH rows have a non-numeric TRANSACTION_AMT")

    is_out = df["TRANSACTION_TP"].isin(config.OUTFLOW_TYPES)
    is_in = df["TRANSACTION_TP"].isin(config.INFLOW_TYPES)
    is_cand = df["TRANSACTION_TP"].isin(config.OTH_CANDIDATE_TYPES)
    df = df[is_out | is_in | is_cand].copy()

    is_out = df["TRANSACTION_TP"].isin(config.OUTFLOW_TYPES)
    is_in = df["TRANSACTION_TP"].isin(config.INFLOW_TYPES)

    # Candidate-directed rows are disbursements, so they behave like outflows.
    df["sender_id"] = df["OTHER_ID"].where(is_in, df["CMTE_ID"])
    df["recip_id"] = df["CMTE_ID"].where(is_in, df["OTHER_ID"])
    df["direction"] = "outflow"
    df.loc[is_in, "direction"] = "inflow"

    flow = df["TRANSACTION_TP"].map(config.FLOW_TYPES)
    df["flow_type"] = flow.map(lambda v: v[0] if isinstance(v, tuple) else "committee_transfer")
    df["support_oppose"] = flow.map(lambda v: v[1] if isinstance(v, tuple) else "support")

    df["source"] = "oth"
    df["sub_id"] = df["SUB_ID"]
    df["recip_cand_id"] = pd.NA
    df["transaction_dt"] = pd.to_datetime(
        df["TRANSACTION_DT"], format="%m%d%Y", errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    df["transaction_tp"] = df["TRANSACTION_TP"]
    df["transaction_pgi"] = df.get("TRANSACTION_PGI")
    df["amndt_ind"] = df["AMNDT_IND"]
    df["memo_cd"] = df["MEMO_CD"]

    df = df[(df["sender_id"].fillna("") != "") & (df["recip_id"].fillna("") != "")]
    cov.record("OTH sender and recipient present", df, f"outflow={int(is_out.sum())} inflow={int(is_in.sum())}")

    self_transfers = int((df["sender_id"] == df["recip_id"]).sum())
    if config.DROP_SELF_TRANSFERS:
        df = df[df["sender_id"] != df["recip_id"]]
        cov.record("OTH self-transfers excluded", df, f"{self_transfers} rows dropped")
    else:
        cov.record("OTH self-transfers kept", df, f"{self_transfers} rows sender == recipient")

    return _finish(df)


def from_pas2(p2c: pd.DataFrame, cov: Coverage) -> pd.DataFrame:
    """usa.fec_pac_to_cand_current -- every row is a disbursement, so CMTE_ID always pays.

    This is the table the handoff correctly called unambiguous: there is no direction
    problem and no both-sides double reporting, because only the payer files it.
    """
    df = p2c.copy()
    df["amount"] = pd.to_numeric(df["TRANSACTION_AMT"], errors="coerce")
    cov.record("PAS2 extracted", df, starts_branch=True)

    bad = int(df["amount"].isna().sum())
    if bad:
        raise ValueError(f"{bad} PAS2 rows have a non-numeric TRANSACTION_AMT")

    flow = df["TRANSACTION_TP"].map(config.FLOW_TYPES)
    df["flow_type"] = flow.map(lambda v: v[0] if isinstance(v, tuple) else pd.NA)
    df["support_oppose"] = flow.map(lambda v: v[1] if isinstance(v, tuple) else pd.NA)
    df = df[df["flow_type"].notna()]
    cov.record("PAS2 known flow type", df)

    df["source"] = "pas2"
    df["sub_id"] = df["SUB_ID"]
    df["sender_id"] = df["CMTE_ID"]
    df["recip_id"] = df["OTHER_ID"]
    df["recip_cand_id"] = df["CAND_ID"].replace("", pd.NA)
    df["transaction_dt"] = pd.to_datetime(
        df["TRANSACTION_DT"], format="%m%d%Y", errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    df["transaction_tp"] = df["TRANSACTION_TP"]
    df["transaction_pgi"] = df.get("TRANSACTION_PGI")
    df["amndt_ind"] = df["AMNDT_IND"]
    df["memo_cd"] = df["MEMO_CD"]

    df = df[(df["sender_id"].fillna("") != "") & (df["recip_id"].fillna("") != "")]
    cov.record("PAS2 sender and recipient present", df)
    return _finish(df)


def dedupe(df: pd.DataFrame, cov: Coverage) -> pd.DataFrame:
    """Remove every way the same real transaction can appear more than once.

    Three distinct duplicate paths, collapsed by two passes:

    1. **Exact same row in both files.** 128,088 OTH rows share a SUB_ID with a PAS2 row
       ($274M). SUB_ID is FEC's own global row key, so this pass is exact, not heuristic.
    2. **Both sides of an OTH transfer.** The payer files it as an outflow and the
       recipient as an inflow, under different SUB_IDs. Normalising to (sender, recip)
       first makes them identical on the fingerprint.
    3. **The same contribution in both files under different SUB_IDs.**

    PAS2 wins ties because it names the candidate; among OTH rows the payer's own filing
    wins, being the more authoritative record of a payment.
    """
    order = {"pas2": 0, "oth": 1}
    out = df.copy()
    out["_pref"] = out["source"].map(order).fillna(9)

    before = len(out)
    out = out.sort_values("_pref", kind="stable").drop_duplicates(subset=["sub_id"], keep="first")
    cov.record("deduped on SUB_ID across sources", out, f"{before - len(out)} exact duplicate rows")

    before = len(out)
    out = out.drop_duplicates(
        subset=["sender_id", "recip_id", "transaction_dt", "amount"], keep="first"
    )
    cov.record(
        "deduped on transaction fingerprint", out,
        f"{before - len(out)} same-transaction rows filed twice",
    )
    return out.drop(columns="_pref")
